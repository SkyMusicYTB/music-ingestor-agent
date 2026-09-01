from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.api.dependencies import CsrfSession, CurrentSession
from app.db.models import DownloadJob, Event, JobReviewOption, RequestTrack
from app.db.models import Request as DbRequest

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str | None = None
    artist: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    album: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def selection_or_edit_required(self) -> ReviewBody:
        if self.option_id is None and all(
            value is None for value in (self.artist, self.title, self.album)
        ):
            raise ValueError("select a review option or provide a metadata correction")
        return self


def _job(item: DownloadJob) -> dict[str, object]:
    return {
        "id": item.id,
        "status": item.status,
        "stage": item.stage,
        "progress": item.progress,
        "retry_count": item.retry_count,
        "source_extractor": item.source_extractor,
        "source_id": item.source_id,
        "warnings": json.loads(item.warnings_json or "[]"),
        "error": (
            {"code": item.error_code, "message": item.error_message} if item.error_code else None
        ),
        "final_relative_path": item.final_relative_path,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


@router.get("")
def list_jobs(request: Request, authenticated: CurrentSession) -> dict[str, object]:
    return {
        "jobs": [_job(job) for job in request.app.state.jobs.list_for_user(authenticated.user_id)]
    }


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, authenticated: CsrfSession) -> dict[str, object]:
    try:
        job = request.app.state.jobs.mutate_for_user(job_id, authenticated.user_id, "cancel")
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return _job(job)


@router.post("/{job_id}/retry")
def retry_job(job_id: str, request: Request, authenticated: CsrfSession) -> dict[str, object]:
    try:
        job = request.app.state.jobs.mutate_for_user(job_id, authenticated.user_id, "retry")
    except LookupError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(error)) from error
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return _job(job)


@router.post("/{job_id}/review")
def review_job(
    job_id: str, body: ReviewBody, request: Request, authenticated: CsrfSession
) -> dict[str, object]:
    with request.app.state.session_factory.begin() as session:
        job = session.scalar(
            select(DownloadJob)
            .join(RequestTrack)
            .join(DbRequest)
            .where(DownloadJob.id == job_id, DbRequest.user_id == authenticated.user_id)
        )
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
        if job.status != "needs_review":
            raise HTTPException(status.HTTP_409_CONFLICT, "job is not awaiting review")
        snapshot = json.loads(job.approved_snapshot_json)
        if body.option_id:
            option = session.scalar(
                select(JobReviewOption).where(
                    JobReviewOption.id == body.option_id, JobReviewOption.job_id == job_id
                )
            )
            if option is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid review option")
            payload = json.loads(option.provider_payload_json)
            allowed = {
                key: payload[key]
                for key in (
                    "artist",
                    "title",
                    "album",
                    "album_artist",
                    "year",
                    "recording_mbid",
                    "release_mbid",
                    "release_group_mbid",
                    "source_id",
                    "source_extractor",
                )
                if key in payload
            }
            snapshot.update(allowed)
            option.selected_at = datetime.now(UTC)
        for field in ("artist", "title", "album"):
            value = getattr(body, field)
            if value is not None:
                snapshot[field] = value.strip()
        if not snapshot.get("artist") or not snapshot.get("title"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "artist and title are required")
        job.approved_snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        job.status = "queued"
        job.stage = "queued"
        job.available_at = datetime.now(UTC)
        job.error_code = None
        job.error_message = None
        session.add(
            Event(
                entity_type="job",
                entity_id=job.id,
                event_type="job.reviewed",
                message="Review selection accepted",
            )
        )
        session.flush()
        return _job(job)
