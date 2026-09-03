from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.responses import Response

from app.api.dependencies import CurrentAdmin, CurrentSession, FragmentSession
from app.api.health import detailed_health_snapshot
from app.api.usage import latest_execution_summary, usage_snapshot
from app.db.models import (
    ArtworkCache,
    JobDecision,
    JobReviewOption,
    SourceCandidate,
)
from app.db.models import Request as DbRequest
from app.repositories.decisions import review_bundle_fingerprint

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
        for key in (
            "label",
            "artist",
            "title",
            "album",
            "channel",
            "source_id",
            "reason",
        )
        if payload.get(key) is not None and str(payload.get(key)).strip()
    ]
    duration = payload.get("duration_seconds")
    duration_seconds = (
        float(duration)
        if isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and 0 < float(duration) <= 14_400
        else None
    )
    return {
        "id": option.id,
        "decision_id": option.decision_id,
        "kind": option.kind,
        "rank": option.rank,
        "score": option.score,
        "label": " · ".join(label_parts)[:800] or f"{option.kind.title()} option {option.rank}",
        "recommended": option.rank == 1,
        "materially_different": option.materially_different,
        "provider": _bounded_display(payload.get("provider")),
        "uploader": _bounded_display(payload.get("uploader") or payload.get("channel")),
        "uploader_relationship": _bounded_display(payload.get("uploader_relationship")),
        "duration_seconds": duration_seconds,
        "version": _bounded_display(payload.get("version") or payload.get("version_signature")),
        "album": _bounded_display(payload.get("album")),
        "year": _bounded_display(payload.get("year")),
        "release_status": _bounded_display(payload.get("release_status") or payload.get("status")),
        "primary_type": _bounded_display(payload.get("primary_type")),
    }


def _bounded_display(value: object, *, limit: int = 300) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized[:limit] or None


def _json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _friendly_stage(value: str) -> str:
    return {
        "queued": "Waiting to start",
        "resolving_source": "Finding the best safe source",
        "waiting_ai": "Confirming the match",
        "downloading": "Downloading audio",
        "resolving_metadata": "Confirming canonical metadata",
        "fetching_artwork": "Finding artwork",
        "tagging": "Writing music tags",
        "verifying": "Checking the finished audio",
        "publishing": "Adding to the library",
        "completed": "Ready in your library",
    }.get(value, value.replace("_", " ").title())


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
def downloads_page(
    request: Request,
    authenticated: FragmentSession,
    view: Literal["visible", "active", "attention", "finished", "hidden"] = "visible",
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=25, le=100),
    fragment: bool = False,
) -> Response:
    event_cursor = _event_cursor(request)
    if page_size not in {25, 50, 100}:
        raise HTTPException(422, "page_size must be 25, 50 or 100")
    result = request.app.state.jobs.page_for_user(
        authenticated.user_id, view=view, page=page, page_size=page_size
    )
    jobs = result.jobs
    job_ids = [job.id for job in jobs]
    reviews: dict[str, list[dict[str, object]]] = {job_id: [] for job_id in job_ids}
    review_bundles: dict[str, dict[str, object]] = {}
    snapshots: dict[str, dict[str, object]] = {}
    warnings: dict[str, list[dict[str, str]]] = {}
    match_details: dict[str, list[dict[str, object]]] = {job_id: [] for job_id in job_ids}
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
        decisions = (
            list(
                session.scalars(
                    select(JobDecision)
                    .where(
                        JobDecision.job_id.in_(job_ids),
                        JobDecision.state.in_(["pending", "selected"]),
                    )
                    .order_by(JobDecision.job_id, JobDecision.category)
                )
            )
            if job_ids
            else []
        )
        source_candidates = (
            list(
                session.scalars(select(SourceCandidate).where(SourceCandidate.job_id.in_(job_ids)))
            )
            if job_ids
            else []
        )
    source_by_id = {candidate.id: candidate for candidate in source_candidates}
    pending_decision_ids = {decision.id for decision in decisions if decision.state == "pending"}
    for job in jobs:
        try:
            raw_snapshot = json.loads(job.approved_snapshot_json)
        except (TypeError, json.JSONDecodeError):
            raw_snapshot = {}
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        snapshots[job.id] = {
            key: snapshot.get(key)
            for key in ("artist", "title", "album", "year", "recording_mbid", "release_mbid")
        }
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
        if option.decision_id in pending_decision_ids:
            option_view = _review_option_view(option)
            if option_view["recommended"] or option_view["materially_different"]:
                reviews.setdefault(option.job_id, []).append(option_view)
    option_decision_ids = {
        option.decision_id for option in options if option.decision_id in pending_decision_ids
    }
    for job in jobs:
        job_id = job.id
        pending = [
            decision
            for decision in decisions
            if decision.job_id == job_id and decision.state == "pending"
        ]
        if pending:
            review_bundles[job_id] = {
                "fingerprint": review_bundle_fingerprint(pending),
                "revision": job.decision_revision,
                "has_options": all(decision.id in option_decision_ids for decision in pending),
                "decisions": [
                    {
                        "id": decision.id,
                        "category": decision.category,
                        "reason_codes": json.loads(decision.reason_codes_json or "[]"),
                    }
                    for decision in pending
                ],
            }
        selected = [
            decision
            for decision in decisions
            if decision.job_id == job_id and decision.state == "selected"
        ]
        for decision in selected:
            payload = _json_object(decision.selected_payload_json)
            detail: dict[str, object] = {
                "category": decision.category,
                "decided_by": decision.decided_by or "deterministic",
                "confidence": (
                    decision.model_confidence
                    if decision.model_confidence is not None
                    else decision.local_confidence
                ),
            }
            if decision.category == "acquisition_source":
                source_id = payload.get("source_candidate_id")
                source = source_by_id.get(source_id) if isinstance(source_id, str) else None
                if source is not None:
                    detail.update(
                        {
                            "provider": source.provider,
                            "uploader": source.uploader,
                            "uploader_relationship": source.uploader_relationship,
                            "duration_seconds": source.duration_seconds,
                        }
                    )
            elif decision.category == "canonical_metadata":
                detail.update(
                    {key: payload.get(key) for key in ("artist", "title", "album", "year")}
                )
            match_details[job_id].append(detail)
    return _render(
        request,
        "downloads_content.html" if fragment else "downloads.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            jobs=jobs,
            result=result,
            reviews=reviews,
            review_bundles=review_bundles,
            snapshots=snapshots,
            warnings=warnings,
            match_details=match_details,
            friendly_stage=_friendly_stage,
        ),
    )


@router.get("/library")
def library_page(
    request: Request,
    authenticated: FragmentSession,
    q: str = Query(default="", max_length=300),
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=50, ge=25, le=100),
    format: str | None = Query(default=None, max_length=16),
    codec: str | None = Query(default=None, max_length=64),
    presence: Literal["present", "missing", "all"] = "present",
    fragment: bool = False,
) -> Response:
    event_cursor = _event_cursor(request)
    if page_size not in {25, 50, 100}:
        raise HTTPException(422, "page_size must be 25, 50 or 100")
    result = request.app.state.library.search(
        q, page, page_size, format=format, codec=codec, presence=presence
    )
    return _render(
        request,
        "library_content.html" if fragment else "library.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            result=result,
            query=q,
            format_filter=format or "",
            codec_filter=codec or "",
            presence_filter=presence,
            scan_status=request.app.state.library.scan_status(
                include_details=authenticated.role == "admin"
            ),
        ),
    )


@router.get("/usage")
def usage_page(
    request: Request, authenticated: CurrentSession, scope: Literal["own", "all", "system"] = "own"
) -> Response:
    event_cursor = _event_cursor(request)
    if scope != "own" and authenticated.role != "admin":
        raise HTTPException(403, "administrator access required")
    aggregates = usage_snapshot(
        request.app.state.session_factory, user_id=authenticated.user_id, scope=scope
    )
    return _render(
        request,
        "usage.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            calls=aggregates["recent"],
            aggregates=aggregates,
            execution_settings=request.app.state.settings,
        ),
    )


@router.get("/settings")
def settings_page(request: Request, authenticated: CurrentAdmin) -> Response:
    event_cursor = _event_cursor(request)
    settings = request.app.state.settings
    visible: dict[str, object] = {
        "Environment": settings.environment,
        "Model": settings.openai_model,
        "Model rounds": settings.max_model_rounds,
        "Built-in tools per response": settings.openai_max_tool_calls,
        "Overall model deadline": f"{settings.max_agent_seconds} seconds",
        "Budget configuration": settings.model_rounds_configuration_source,
        "Web search": settings.openai_web_search_enabled,
        "Music library": str(settings.music_path),
        "Downloads": str(settings.downloads_path),
        "Maximum media duration": f"{settings.max_direct_media_seconds // 60} minutes",
        "Automatic exact Add": settings.auto_download_exact_single,
        "Browser origin policy": settings.origin_policy,
        "Public base URL": settings.public_base_url or "Not fixed; derived from each request",
        "Cookie security": "Secure whenever the effective request scheme is HTTPS",
        "Media source policy": settings.media_source_policy,
        "Enabled media providers": ", ".join(settings.enabled_media_providers),
        "Review policy": settings.review_policy,
        "SQLite journal": "DELETE / synchronous FULL",
    }
    execution = latest_execution_summary(request.app.state.session_factory)
    if execution is None:
        visible["Last recorded model execution"] = "No completed execution recorded"
    else:
        visible["Last execution termination"] = execution["termination_reason"]
        visible["Last execution rounds"] = (
            f"{execution['model_rounds_used']} / {execution['configured_model_rounds']}"
        )
        visible["Last execution built-in tool cap"] = execution["configured_tool_calls"]
        visible["Last execution deadline"] = (
            f"{execution['configured_agent_seconds']} seconds"
            if execution["configured_agent_seconds"] is not None
            else "Not recorded"
        )
        visible["Last execution recorded at"] = execution["recorded_at"]
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
def health_page(request: Request, authenticated: CurrentAdmin) -> Response:
    event_cursor = _event_cursor(request)
    healthy, checks = detailed_health_snapshot(request)
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
