from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    DownloadJob,
    Event,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    SourceCandidate,
)
from app.services.duplicates import normalize_text
from app.sources import EXECUTABLE_EVIDENCE_KINDS

_ACQUISITION_PROVIDERS = ("bandcamp", "soundcloud", "youtube")


def _provider_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item for item in value if isinstance(item, str) and item in _ACQUISITION_PROVIDERS
        )
    )


def _snapshot(track: RequestTrack) -> dict[str, object]:
    try:
        metadata_provenance = json.loads(track.metadata_provenance_json or "{}")
    except json.JSONDecodeError:
        metadata_provenance = {}
    if not isinstance(metadata_provenance, dict):
        metadata_provenance = {}
    raw_constraints = metadata_provenance.get("request_constraints")
    request_constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
    requested_provider = request_constraints.get("requested_provider")
    if requested_provider not in _ACQUISITION_PROVIDERS:
        requested_provider = None
    requested_providers = _provider_list(request_constraints.get("requested_providers"))
    if requested_provider is not None and requested_provider not in requested_providers:
        requested_providers.append(requested_provider)
    excluded_providers = _provider_list(request_constraints.get("excluded_providers"))
    requested_providers = [
        provider for provider in requested_providers if provider not in excluded_providers
    ]
    requested_album = request_constraints.get("requested_album")
    if not isinstance(requested_album, str) or not requested_album.strip():
        requested_album = None
    requested_version = request_constraints.get("requested_version")
    if not isinstance(requested_version, str) or not requested_version.strip():
        requested_version = None
    return {
        "request_track_id": track.id,
        "artist": track.artist,
        "title": track.title,
        "album": track.album,
        "album_artist": track.album_artist,
        "year": track.year,
        "duration_seconds": track.duration_seconds,
        "recording_mbid": track.recording_mbid,
        "release_mbid": track.release_mbid,
        "release_group_mbid": track.release_group_mbid,
        "canonical_identity_verified": track.canonical_identity_verified,
        "metadata_provenance": metadata_provenance,
        "source_url": track.source_url,
        "source_extractor": track.source_extractor,
        "source_id": track.source_id,
        "version_signature": track.version_signature,
        "metadata_confidence": track.metadata_confidence,
        "requested_provider": requested_provider,
        "requested_providers": requested_providers,
        "excluded_providers": excluded_providers,
        "provider_fallback_allowed": False,
        "requested_album": requested_album,
        "requested_version": requested_version,
    }


def dedup_key(track: RequestTrack) -> str:
    if track.source_extractor and track.source_id:
        value = f"source:{track.source_extractor}:{track.source_id}"
    elif track.canonical_identity_verified and track.recording_mbid:
        value = f"mbid:{track.recording_mbid}:{track.version_signature}"
    else:
        value = (
            f"text:{normalize_text(track.artist)}:{normalize_text(track.title)}:"
            f"{track.version_signature}"
        )
    return hashlib.sha256(value.encode()).hexdigest()


class JobRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def queue_approved(self, request_id: str, user_id: str, track_ids: list[str]) -> list[str]:
        now = datetime.now(UTC)
        job_ids: list[str] = []
        try:
            with self.factory.begin() as session:
                request = session.scalar(
                    select(Request).where(Request.id == request_id, Request.user_id == user_id)
                )
                if request is None:
                    raise LookupError("request not found")
                tracks = list(
                    session.scalars(
                        select(RequestTrack).where(
                            RequestTrack.request_id == request_id, RequestTrack.id.in_(track_ids)
                        )
                    )
                )
                if len(tracks) != len(track_ids):
                    raise ValueError("one or more proposal tracks are invalid")
                for track in tracks:
                    if track.duplicate_status == "owned":
                        continue
                    existing = session.scalar(
                        select(DownloadJob.id).where(DownloadJob.request_track_id == track.id)
                    )
                    if existing:
                        job_ids.append(existing)
                        continue
                    track.approved_at = now
                    job = DownloadJob(
                        request_track_id=track.id,
                        approved_snapshot_json=json.dumps(
                            _snapshot(track), ensure_ascii=False, separators=(",", ":")
                        ),
                        dedup_key=dedup_key(track),
                        status="queued",
                        stage="queued",
                    )
                    session.add(job)
                    session.flush()
                    approved_source = session.scalar(
                        select(SourceCandidate)
                        .outerjoin(
                            EvidenceReference,
                            SourceCandidate.evidence_id == EvidenceReference.id,
                        )
                        .where(
                            SourceCandidate.request_track_id == track.id,
                            or_(
                                SourceCandidate.evidence_id.is_(None),
                                EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                            ),
                            SourceCandidate.policy_status == "allowed",
                            SourceCandidate.probe_status == "valid",
                        )
                        .order_by(SourceCandidate.local_score.desc())
                        .limit(1)
                    )
                    if approved_source is not None:
                        approved_source.job_id = job.id
                        job.active_source_candidate_id = approved_source.id
                    job_ids.append(job.id)
                    session.add(
                        Event(
                            entity_type="job",
                            entity_id=job.id,
                            event_type="job.queued",
                            message=f"Queued {track.artist} — {track.title}",
                        )
                    )
                request.selected_count = len(job_ids)
                request.status = "queued"
            return job_ids
        except IntegrityError as error:
            raise ValueError("an equivalent acquisition is already active") from error

    def list_for_user(self, user_id: str, limit: int = 100) -> list[DownloadJob]:
        with self.factory() as session:
            return list(
                session.scalars(
                    select(DownloadJob)
                    .join(RequestTrack, DownloadJob.request_track_id == RequestTrack.id)
                    .join(Request, RequestTrack.request_id == Request.id)
                    .where(Request.user_id == user_id)
                    .order_by(DownloadJob.created_at.desc())
                    .limit(limit)
                )
            )

    def mutate_for_user(self, job_id: str, user_id: str, operation: str) -> DownloadJob:
        with self.factory.begin() as session:
            job = session.scalar(
                select(DownloadJob)
                .join(RequestTrack)
                .join(Request)
                .where(DownloadJob.id == job_id, Request.user_id == user_id)
            )
            if job is None:
                raise LookupError("job not found")
            if operation == "cancel":
                if job.status in {"completed", "cancelled", "failed"}:
                    raise ValueError("job is not cancellable")
                timestamp = datetime.now(UTC)
                job.cancel_requested_at = timestamp
                if job.status == "needs_review" and job.lease_token is None:
                    job.status = "cancelled"
                    job.completed_at = timestamp
                    job.error_code = None
                    job.error_message = None
                else:
                    job.status = "cancel_requested"
            elif operation == "retry":
                if job.status == "needs_review":
                    pending_ids = list(
                        session.scalars(
                            select(JobDecision.id).where(
                                JobDecision.job_id == job.id,
                                JobDecision.state == "pending",
                            )
                        )
                    )
                    option_decision_ids = (
                        set(
                            session.scalars(
                                select(JobReviewOption.decision_id).where(
                                    JobReviewOption.decision_id.in_(pending_ids)
                                )
                            )
                        )
                        if pending_ids
                        else set()
                    )
                    if pending_ids and all(
                        decision_id in option_decision_ids for decision_id in pending_ids
                    ):
                        raise ValueError(
                            "this job needs its focused review decision before it can resume"
                        )
                    job.warnings_json = _without_review_warning(job.warnings_json)
                if job.status not in {"failed", "waiting_for_space"}:
                    if job.status != "needs_review":
                        raise ValueError("job is not retryable")
                job.status = "queued"
                job.stage = "queued"
                job.available_at = datetime.now(UTC)
                job.error_code = None
                job.error_message = None
            else:
                raise ValueError("unsupported job operation")
            session.add(
                Event(
                    entity_type="job",
                    entity_id=job.id,
                    event_type=f"job.{operation}",
                    message=f"Job {operation} requested",
                )
            )
            session.flush()
            return job


def _without_review_warning(value: str | None) -> str:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return "[]"
    if not isinstance(decoded, list):
        return "[]"
    retained = [
        item
        for item in decoded
        if not (isinstance(item, dict) and item.get("code") == "needs_review")
    ]
    return json.dumps(retained[-20:], ensure_ascii=False, separators=(",", ":"))
