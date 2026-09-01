from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DownloadJob, Event, Request, RequestTrack
from app.services.duplicates import normalize_text


def _snapshot(track: RequestTrack) -> dict[str, object]:
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
        "source_url": track.source_url,
        "source_extractor": track.source_extractor,
        "source_id": track.source_id,
        "version_signature": track.version_signature,
        "metadata_confidence": track.metadata_confidence,
    }


def dedup_key(track: RequestTrack) -> str:
    if track.source_extractor and track.source_id:
        value = f"source:{track.source_extractor}:{track.source_id}"
    elif track.recording_mbid:
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
                job.status = "cancel_requested"
                job.cancel_requested_at = datetime.now(UTC)
            elif operation == "retry":
                if job.status not in {"failed", "needs_review", "waiting_for_space"}:
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
