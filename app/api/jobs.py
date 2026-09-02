from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.dependencies import CsrfSession, CurrentSession
from app.db.models import DownloadJob, Event, RequestTrack
from app.db.models import Request as DbRequest
from app.repositories.decisions import (
    DecisionConflict,
    DecisionSelection,
    apply_review_bundle,
)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class ReviewCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artist: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    album: str | None = Field(default=None, max_length=300)


class ReviewSelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(min_length=1, max_length=36)
    option_id: str = Field(min_length=1, max_length=36)
    correction: ReviewCorrection | None = None


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    selections: list[ReviewSelectionBody] = Field(min_length=1, max_length=4)


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
        selections = [
            DecisionSelection(
                decision_id=item.decision_id,
                option_id=item.option_id,
                correction=(item.correction.model_dump() if item.correction is not None else None),
            )
            for item in body.selections
        ]
        try:
            result = apply_review_bundle(
                session,
                job,
                bundle_fingerprint=body.bundle_fingerprint,
                revision=body.revision,
                selections=selections,
            )
        except DecisionConflict as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        session.add(
            Event(
                entity_type="job",
                entity_id=job.id,
                event_type="job.reviewed",
                message=(
                    "Review selection replayed" if result.replayed else "Review selection accepted"
                ),
                details_json=json.dumps(
                    {
                        "bundle_fingerprint": body.bundle_fingerprint,
                        "revision": body.revision,
                        "replayed": result.replayed,
                    },
                    separators=(",", ":"),
                ),
            )
        )
        session.flush()
        return _job(result.job)
