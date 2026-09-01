from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select

from app.api.dependencies import CsrfSession, CurrentSession
from app.db.models import ServiceTask

router = APIRouter(prefix="/api/v1/library", tags=["library"])


@router.get("")
def get_library(
    request: Request,
    authenticated: CurrentSession,
    q: str = Query(default="", max_length=300),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    result = request.app.state.library.search(q, page, page_size)
    return {
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "items": [
            {
                "id": item.id,
                "artist": item.artist,
                "title": item.title,
                "album": item.album,
                "year": item.year,
                "duration_seconds": item.duration_seconds,
                "filepath": item.filepath,
                "codec": item.codec,
                "is_present": item.is_present,
            }
            for item in result.items
        ],
    }


@router.post("/rescan", status_code=status.HTTP_202_ACCEPTED)
def rescan_library(request: Request, authenticated: CsrfSession) -> dict[str, object]:
    with request.app.state.session_factory.begin() as session:
        active = session.scalar(
            select(ServiceTask.id).where(
                ServiceTask.target == "worker",
                ServiceTask.kind == "library_scan",
                ServiceTask.state.in_(["queued", "running", "retry_wait"]),
            )
        )
        if active:
            raise HTTPException(status.HTTP_409_CONFLICT, "a library scan is already queued")
        task = ServiceTask(
            target="worker",
            kind="library_scan",
            payload_json=json.dumps(
                {"full": True, "requested_by": authenticated.user_id}, separators=(",", ":")
            ),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        return {"task_id": task.id, "status": "queued"}
