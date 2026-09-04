from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import immediate_session
from app.db.models import (
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    SourceCandidate,
)
from app.repositories.events import make_event
from app.services.artist_credits import structured_artists
from app.services.duplicates import normalize_text, normalize_version_signature
from app.services.metadata_review_repair import repair_empty_metadata_review
from app.sources import EXECUTABLE_EVIDENCE_KINDS

_ACQUISITION_PROVIDERS = ("bandcamp", "soundcloud", "youtube")
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_VIEWS = ("visible", "active", "attention", "finished", "hidden")


@dataclass(frozen=True)
class JobPage:
    jobs: list[DownloadJob]
    page: int
    page_size: int
    total: int
    counts: dict[str, int]
    view: str

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


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
        "artists": list(structured_artists(metadata_provenance.get("artists"))),
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
        "version_signature": normalize_version_signature(track.version_signature),
        "metadata_confidence": track.metadata_confidence,
        "requested_provider": requested_provider,
        "requested_providers": requested_providers,
        "excluded_providers": excluded_providers,
        "provider_fallback_allowed": False,
        "requested_album": requested_album,
        "requested_version": requested_version,
    }


def dedup_key(track: RequestTrack) -> str:
    version = normalize_version_signature(track.version_signature)
    if track.source_extractor and track.source_id:
        value = f"source:{track.source_extractor}:{track.source_id}"
    elif track.canonical_identity_verified and track.recording_mbid:
        value = f"mbid:{track.recording_mbid}:{version}"
    else:
        value = f"text:{normalize_text(track.artist)}:{normalize_text(track.title)}:{version}"
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
                    track.version_signature = normalize_version_signature(track.version_signature)
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
                        make_event(
                            session,
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

    def page_for_user(
        self, user_id: str, *, view: str = "visible", page: int = 1, page_size: int = 50
    ) -> JobPage:
        if view not in JOB_VIEWS or page_size not in {25, 50, 100} or page < 1:
            raise ValueError("invalid download view or pagination")
        visible = DownloadJob.dismissed_at.is_(None)
        filters = {
            "visible": visible,
            "active": visible
            & DownloadJob.status.not_in(TERMINAL_STATUSES | {"needs_review", "waiting_for_space"}),
            "attention": visible
            & DownloadJob.status.in_({"needs_review", "waiting_for_space", "failed"}),
            "finished": visible & DownloadJob.status.in_(TERMINAL_STATUSES),
            "hidden": DownloadJob.dismissed_at.is_not(None),
        }
        with self.factory() as session:
            base = (
                select(DownloadJob)
                .join(RequestTrack)
                .join(Request)
                .where(Request.user_id == user_id)
            )
            counts = {
                name: int(
                    session.scalar(
                        select(func.count()).select_from(base.where(predicate).subquery())
                    )
                    or 0
                )
                for name, predicate in filters.items()
            }
            total = counts[view]
            page = min(page, max(1, (total + page_size - 1) // page_size))
            jobs = list(
                session.scalars(
                    base.where(filters[view])
                    .order_by(
                        case((DownloadJob.status.in_(TERMINAL_STATUSES), 1), else_=0),
                        DownloadJob.created_at.desc(),
                        DownloadJob.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return JobPage(jobs, page, page_size, total, counts, view)

    def clear_finished(self, user_id: str, statuses: list[str] | None = None) -> int:
        selected = set(statuses) if statuses is not None else set(TERMINAL_STATUSES)
        if (
            not selected
            or not selected <= TERMINAL_STATUSES
            or (statuses is not None and len(selected) != len(statuses))
        ):
            raise ValueError("choose a unique subset of completed, failed and cancelled")
        with immediate_session(self.factory) as session:
            owned_tracks = select(RequestTrack.id).join(Request).where(Request.user_id == user_id)
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.request_track_id.in_(owned_tracks),
                    DownloadJob.status.in_(selected),
                    DownloadJob.dismissed_at.is_(None),
                )
                .values(dismissed_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            count = int(result.rowcount)
            if count:
                session.add(
                    make_event(
                        session,
                        entity_type="job",
                        event_type="job.history_cleared",
                        message="Finished downloads removed from your list",
                        audience="user",
                        user_id=user_id,
                        details={"count": count},
                    )
                )
            return count

    def mutate_for_user(self, job_id: str, user_id: str, operation: str) -> DownloadJob:
        with immediate_session(self.factory) as session:
            job = session.scalar(
                select(DownloadJob)
                .join(RequestTrack)
                .join(Request)
                .where(DownloadJob.id == job_id, Request.user_id == user_id)
            )
            if job is None:
                raise LookupError("job not found")
            if operation == "dismiss":
                if job.status not in TERMINAL_STATUSES:
                    raise ValueError("only finished jobs can be removed from the list")
                if job.dismissed_at is not None:
                    return job
                job.dismissed_at = datetime.now(UTC)
            elif operation == "restore":
                if job.dismissed_at is None:
                    return job
                job.dismissed_at = None
            elif operation == "cancel":
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
                repaired_metadata_review = False
                if job.status == "needs_review":
                    repaired_metadata_review = repair_empty_metadata_review(session, job)
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
                if (
                    job.status not in {"failed", "waiting_for_space"}
                    and not repaired_metadata_review
                ):
                    if job.status != "needs_review":
                        raise ValueError("job is not retryable")
                job.status = "queued"
                job.stage = "resolving_metadata" if repaired_metadata_review else "queued"
                job.available_at = datetime.now(UTC)
                job.error_code = None
                job.error_message = None
                job.dismissed_at = None
            else:
                raise ValueError("unsupported job operation")
            session.add(
                make_event(
                    session,
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
