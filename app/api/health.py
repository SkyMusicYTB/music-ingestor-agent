from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from starlette.responses import Response

from app.api.dependencies import CurrentSession
from app.db.engine import EXPECTED_SCHEMA_REVISION, current_revision
from app.db.models import ServiceHeartbeat

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


def health_snapshot(request: Request) -> tuple[bool, dict[str, object]]:
    settings = request.app.state.settings
    checks: dict[str, object] = {}
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
        revision = current_revision(request.app.state.engine)
        checks["database"] = {
            "ok": revision == EXPECTED_SCHEMA_REVISION,
            "revision": revision,
            "expected": EXPECTED_SCHEMA_REVISION,
            "journal_mode": "DELETE",
        }
    except Exception as error:
        checks["database"] = {"ok": False, "error": str(error)[:200]}
    checks["paths"] = {
        "ok": all(
            path.exists() and os.access(path, os.R_OK | os.W_OK)
            for path in (
                settings.database_path.parent,
                settings.artwork_path,
                settings.downloads_path,
            )
        ),
        "music_readable": settings.music_path.exists() and os.access(settings.music_path, os.R_OK),
    }
    scan_complete = request.app.state.library.initial_scan_complete()
    checks["initial_scan"] = {
        "ok": scan_complete or not settings.initial_scan_required,
        "required": settings.initial_scan_required,
        "completed": scan_complete,
    }
    with request.app.state.session_factory() as session:
        heartbeats = list(session.scalars(select(ServiceHeartbeat)))
    now = datetime.now(UTC)
    heartbeat_details: dict[str, object] = {}
    for heartbeat in heartbeats:
        timestamp = heartbeat.last_heartbeat_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        heartbeat_details[heartbeat.service] = {
            "ok": now - timestamp <= timedelta(seconds=180),
            "version": heartbeat.service_version,
            "last_heartbeat_at": timestamp.isoformat(),
            "active_work_count": heartbeat.active_work_count,
        }
    checks["services"] = heartbeat_details
    ready = all(
        bool(value.get("ok"))
        for key, value in checks.items()
        if key in {"database", "paths", "initial_scan"} and isinstance(value, dict)
    )
    return ready, checks


@router.get("/health/ready")
def ready(request: Request) -> Response:
    healthy, _checks = health_snapshot(request)
    return JSONResponse(
        {"status": "ready" if healthy else "not_ready"},
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@router.get("/api/v1/health")
def detailed_health(request: Request, authenticated: CurrentSession) -> dict[str, object]:
    healthy, checks = health_snapshot(request)
    return {"status": "ready" if healthy else "not_ready", "checks": checks}
