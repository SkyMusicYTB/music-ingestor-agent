from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.responses import Response

from app.api.dependencies import CurrentSession
from app.api.health import health_snapshot
from app.api.usage import usage_snapshot
from app.db.models import ArtworkCache, JobReviewOption, OpenAICall
from app.db.models import Request as DbRequest

router = APIRouter()
_CACHE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def _context(
    request: Request,
    authenticated: CurrentSession,
    *,
    event_cursor: int,
    **values: object,
) -> dict[str, object]:
    return {
        "user": authenticated,
        "csrf_token": request.cookies.get("music_agent_csrf", ""),
        "app_version": request.app.state.settings.app_version,
        "event_cursor": event_cursor,
        **values,
    }


def _render(request: Request, name: str, context: dict[str, object]) -> Response:
    return cast(
        Response,
        request.app.state.templates.TemplateResponse(request=request, name=name, context=context),
    )


def _event_cursor(request: Request) -> int:
    """Capture replay position before a page reads any state it renders."""

    _minimum, maximum = request.app.state.events.bounds()
    return maximum or 0


def _review_option_view(option: JobReviewOption) -> dict[str, object]:
    try:
        raw = json.loads(option.provider_payload_json)
    except (TypeError, json.JSONDecodeError):
        raw = {}
    payload = raw if isinstance(raw, dict) else {}
    label_parts = [
        str(payload.get(key)).strip()
        for key in ("artist", "title", "album", "channel", "source_id", "reason")
        if payload.get(key) is not None and str(payload.get(key)).strip()
    ]
    return {
        "id": option.id,
        "kind": option.kind,
        "rank": option.rank,
        "score": option.score,
        "label": " · ".join(label_parts)[:800] or f"{option.kind.title()} option {option.rank}",
    }


@router.get("/")
def home(request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    with request.app.state.session_factory() as session:
        recent = list(
            session.scalars(
                select(DbRequest)
                .where(DbRequest.user_id == authenticated.user_id)
                .order_by(DbRequest.created_at.desc())
                .limit(12)
            )
        )
    return _render(
        request,
        "index.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            recent_requests=recent,
            library_summary=request.app.state.library.summary(),
        ),
    )


@router.get("/requests/{request_id}")
def request_page(request_id: str, request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    item = request.app.state.requests.get_for_user(request_id, authenticated.user_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    tracks = request.app.state.requests.tracks_for_request(request_id)
    return _render(
        request,
        "request.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            item=item,
            tracks=tracks,
        ),
    )


@router.get("/downloads")
def downloads_page(request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    jobs = request.app.state.jobs.list_for_user(authenticated.user_id)
    job_ids = [job.id for job in jobs]
    reviews: dict[str, list[dict[str, object]]] = {job_id: [] for job_id in job_ids}
    snapshots: dict[str, dict[str, object]] = {}
    warnings: dict[str, list[dict[str, str]]] = {}
    with request.app.state.session_factory() as session:
        options = (
            list(
                session.scalars(
                    select(JobReviewOption)
                    .where(JobReviewOption.job_id.in_(job_ids))
                    .order_by(JobReviewOption.job_id, JobReviewOption.kind, JobReviewOption.rank)
                )
            )
            if job_ids
            else []
        )
    for job in jobs:
        try:
            raw_snapshot = json.loads(job.approved_snapshot_json)
        except (TypeError, json.JSONDecodeError):
            raw_snapshot = {}
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        snapshots[job.id] = {key: snapshot.get(key) for key in ("artist", "title", "album")}
        try:
            raw_warnings = json.loads(job.warnings_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_warnings = []
        warnings[job.id] = [
            {
                "code": str(item.get("code") or "warning"),
                "message": str(item["message"])[:500],
            }
            for item in raw_warnings
            if isinstance(item, dict) and item.get("message")
        ][:20]
    for option in options:
        reviews.setdefault(option.job_id, []).append(_review_option_view(option))
    return _render(
        request,
        "downloads.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            jobs=jobs,
            reviews=reviews,
            snapshots=snapshots,
            warnings=warnings,
        ),
    )


@router.get("/library")
def library_page(
    request: Request,
    authenticated: CurrentSession,
    q: str = Query(default="", max_length=300),
    page: int = Query(default=1, ge=1),
) -> Response:
    event_cursor = _event_cursor(request)
    result = request.app.state.library.search(q, page, 50)
    return _render(
        request,
        "library.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            result=result,
            query=q,
        ),
    )


@router.get("/usage")
def usage_page(request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    with request.app.state.session_factory() as session:
        calls = list(
            session.scalars(select(OpenAICall).order_by(OpenAICall.created_at.desc()).limit(200))
        )
    return _render(
        request,
        "usage.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            calls=calls,
            aggregates=usage_snapshot(request.app.state.session_factory),
        ),
    )


@router.get("/settings")
def settings_page(request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    settings = request.app.state.settings
    visible = {
        "Environment": settings.environment,
        "Model": settings.openai_model,
        "Web search": settings.openai_web_search_enabled,
        "Music library": str(settings.music_path),
        "Downloads": str(settings.downloads_path),
        "Maximum media duration": f"{settings.max_direct_media_seconds // 60} minutes",
        "Automatic exact Add": settings.auto_download_exact_single,
        "HTTP cookie security": "Secure" if settings.https_enabled else "LAN HTTP (not Secure)",
        "SQLite journal": "DELETE / synchronous FULL",
    }
    return _render(
        request,
        "settings.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            settings_visible=visible,
        ),
    )


@router.get("/health")
def health_page(request: Request, authenticated: CurrentSession) -> Response:
    event_cursor = _event_cursor(request)
    healthy, checks = health_snapshot(request)
    return _render(
        request,
        "health.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            healthy=healthy,
            checks=checks,
        ),
    )


@router.get("/artwork/{cache_key}")
def artwork(cache_key: str, request: Request, authenticated: CurrentSession) -> Response:
    if not _CACHE_KEY.fullmatch(cache_key):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    with request.app.state.session_factory() as session:
        cached = session.scalar(select(ArtworkCache).where(ArtworkCache.cache_key == cache_key))
    if cached is None or not cached.relative_path or cached.status != "ok":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    root = request.app.state.settings.artwork_path.resolve()
    relative = Path(cached.relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
    except OSError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND) from error
    return FileResponse(
        path,
        media_type=cached.mime_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )
