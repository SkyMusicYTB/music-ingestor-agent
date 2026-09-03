from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from starlette.responses import Response

from app.api.dependencies import CurrentAdmin
from app.api.usage import latest_execution_summary
from app.db.engine import EXPECTED_SCHEMA_REVISION, current_revision
from app.db.models import ServiceHeartbeat

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "live"}


def health_snapshot(request: Request) -> tuple[bool, dict[str, object]]:
    settings = request.app.state.settings
    checks: dict[str, object] = {}
    database_ready = False
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            journal_mode = str(connection.exec_driver_sql("PRAGMA journal_mode").scalar_one())
            synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()
        revision = current_revision(request.app.state.engine)
        database_ready = (
            revision == EXPECTED_SCHEMA_REVISION and journal_mode == "delete" and synchronous == 2
        )
        checks["database"] = {
            "ok": database_ready,
            "revision": revision,
            "expected": EXPECTED_SCHEMA_REVISION,
            "journal_mode": journal_mode.upper(),
        }
    except Exception as error:
        checks["database"] = {"ok": False, "error": str(error)[:200]}
    checks["paths"] = {
        "ok": all(
            path.exists() and os.access(path, os.R_OK | os.W_OK)
            for path in (
                settings.database_path.parent,
                settings.artwork_path,
            )
        ),
        "music_readable": settings.music_path.exists() and os.access(settings.music_path, os.R_OK),
    }
    # Acquisition has its own baseline gate. A healthy web service must remain
    # available to show scan progress, including on a large first installation.
    # The web sandbox deliberately has no write access to worker download paths.
    scan_complete = False
    heartbeats = []
    if database_ready:
        try:
            scan_complete = request.app.state.library.initial_scan_complete()
            with request.app.state.session_factory() as session:
                heartbeats = list(session.scalars(select(ServiceHeartbeat)))
        except Exception:
            database_ready = False
            checks["database"] = {"ok": False, "error": "health query unavailable"}
    checks["initial_scan"] = {
        "ok": scan_complete or not settings.initial_scan_required,
        "required": settings.initial_scan_required,
        "completed": scan_complete,
    }
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
    checks["model_execution"] = {
        "model": settings.openai_model,
        "model_rounds": settings.max_model_rounds,
        "built_in_tool_calls": settings.openai_max_tool_calls,
        "deadline_seconds": settings.max_agent_seconds,
        "compatibility": settings.model_rounds_configuration_source,
    }
    paths = checks["paths"]
    ready = (
        database_ready
        and isinstance(paths, dict)
        and bool(paths.get("ok"))
        and bool(paths.get("music_readable"))
    )
    return ready, checks


@router.get("/health/ready")
def ready(request: Request) -> Response:
    healthy, _checks = health_snapshot(request)
    return JSONResponse(
        {"status": "ready" if healthy else "not_ready"},
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def detailed_health_snapshot(request: Request) -> tuple[bool, dict[str, object]]:
    """Enrich authenticated diagnostics without changing public readiness work."""
    healthy, checks = health_snapshot(request)
    database = checks.get("database")
    if isinstance(database, dict) and database.get("ok"):
        try:
            checks["last_model_execution"] = latest_execution_summary(
                request.app.state.session_factory
            )
        except Exception:
            checks["last_model_execution"] = {"available": False}
    return healthy, checks


@router.get("/api/v1/health")
def detailed_health(request: Request, authenticated: CurrentAdmin) -> dict[str, object]:
    healthy, checks = detailed_health_snapshot(request)
    return {"status": "ready" if healthy else "not_ready", "checks": checks}
