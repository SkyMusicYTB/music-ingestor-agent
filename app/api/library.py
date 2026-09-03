from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select, text

from app.api.dependencies import AdminCsrfSession, BackgroundSession, CurrentAdmin, CurrentSession
from app.db.models import ScanRun, ServiceTask
from app.repositories.library import scan_payload
from app.services.library_audit import audit_library

router = APIRouter(prefix="/api/v1/library", tags=["library"])


@router.get("/audit")
def library_audit(
    request: Request,
    authenticated: CurrentAdmin,
    verbose: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, object]:
    return audit_library(
        request.app.state.session_factory,
        request.app.state.settings.music_path,
        verbose=verbose,
        limit=limit,
    )


@router.get("")
def get_library(
    request: Request,
    authenticated: CurrentSession,
    q: str = Query(default="", max_length=300),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=1, le=100),
    format: str | None = Query(default=None, max_length=16, pattern=r"^\.?[a-zA-Z0-9]+$"),
    codec: str | None = Query(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_]+$"),
    presence: str = Query(default="present", pattern="^(present|missing|all)$"),
) -> dict[str, object]:
    if page_size not in {25, 50, 100}:
        raise HTTPException(422, "page_size must be 25, 50 or 100")
    result = request.app.state.library.search(
        q, page, page_size, format=format, codec=codec, presence=presence
    )
    return {
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
        "format_counts": result.format_counts,
        "codec_counts": result.codec_counts,
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
                "file_extension": item.file_extension,
                "container": item.container,
                "is_present": item.is_present,
            }
            for item in result.items
        ],
    }


@router.post("/rescan", status_code=status.HTTP_202_ACCEPTED)
def rescan_library(request: Request, authenticated: AdminCsrfSession) -> dict[str, object]:
    with request.app.state.session_factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
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
        task_id = task.id
        session.commit()
        return {"task_id": task_id, "status": "queued", "status_url": "/api/v1/library/scan-status"}


@router.get("/scan-status")
def scan_status(request: Request, authenticated: BackgroundSession) -> dict[str, object]:
    return dict(
        request.app.state.library.scan_status(include_details=authenticated.role == "admin")
    )


@router.get("/scans")
def scans(
    request: Request, authenticated: CurrentAdmin, page: int = Query(default=1, ge=1, le=1_000_000)
) -> dict[str, object]:
    with request.app.state.session_factory() as session:
        rows = list(
            session.scalars(
                select(ScanRun)
                .order_by(ScanRun.generation.desc())
                .offset((page - 1) * 25)
                .limit(25)
            )
        )
    return {"page": page, "items": [scan_payload(scan, include_details=True) for scan in rows]}
