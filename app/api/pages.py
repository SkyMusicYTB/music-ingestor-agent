from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import case, func, or_, select
from sqlalchemy.sql.elements import ColumnElement
from starlette.responses import Response

from app.api.dependencies import CurrentAdmin, CurrentSession, FragmentSession
from app.api.health import detailed_health_snapshot
from app.api.usage import latest_execution_summary, usage_snapshot
from app.db.models import (
    ArtworkCache,
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    SourceCandidate,
)
from app.db.models import Request as DbRequest
from app.repositories.decisions import review_bundle_fingerprint
from app.sources import EXECUTABLE_EVIDENCE_KINDS

router = APIRouter()
_CACHE_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_ATTEMPT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_TERMINATION_LABELS = {
    "normal_synthesis": "Completed with the final proposal",
    "forced_final_synthesis": "Completed with an early final proposal",
    "no_progress_synthesis": "Completed after discovery stopped making progress",
    "model_round_exhaustion": "Stopped at the model-round limit",
    "wall_time_exhaustion": "Stopped at the overall time limit",
    "provider_failure": "Stopped after a model-provider failure",
    "refused": "The model declined the request",
    "malformed_response": "The model returned an unusable response",
    "cancelled": "Cancelled",
    "lease_lost": "Stopped after the task lease changed",
}
_DISCOVERY_REJECTION_LABELS = {
    "provider_search_rejected": "provider searches rejected by policy",
    "transient_provider_search": "temporary provider-search failures",
    "transient_evidence_probe": "temporary evidence-probe failures",
    "transient_candidate_probe": "temporary candidate-probe failures",
    "malformed_search_result": "malformed results",
    "invalid_flat_candidate": "invalid search candidates",
    "duplicate_source_id": "duplicate source identities",
    "probe_rejected": "rejected probes",
    "invalid_probe_candidate": "invalid probe candidates",
    "exhausted_source_id": "previously exhausted sources",
    "probe_budget_exhausted": "probe budget exhausted",
}
_RANKING_FACT_LABELS = {
    "canonical_match": "recording identity",
    "requested_version": "requested version",
    "duration_compatibility": "duration",
    "audio_availability_quality": "audio quality",
    "uploader_relationship": "uploader relationship",
    "provider_reliability": "provider reliability",
    "provider_preference": "provider preference",
}
_SOURCE_CONTRADICTION_LABELS = {
    "artist_credit_mismatch": "incomplete or different artist credit",
    "unrequested_live": "unrequested live version",
    "unrequested_remix": "unrequested remix",
    "unrequested_cover": "unrequested cover",
    "unrequested_karaoke": "unrequested karaoke version",
    "other_artist": "different performing artist",
}
_DIAGNOSTIC_QUERY_SECRET = re.compile(
    r"(?:https?://|\b(?:authorization|bearer|cookie|password|api[_ -]?key|token)\s*[:=])",
    re.IGNORECASE,
)
_PROVIDER_LABELS = {
    "automatic": "Automatic",
    "bandcamp": "Bandcamp",
    "soundcloud": "SoundCloud",
    "youtube": "YouTube",
}
_DIAGNOSTIC_PROVIDERS = frozenset({"bandcamp", "soundcloud", "youtube"})
_SOURCE_SEARCH_PREFIXES = tuple(
    f"{_PROVIDER_LABELS[provider]} search:" for provider in sorted(_DIAGNOSTIC_PROVIDERS)
)
_SOURCE_FAILURE_LABELS = {
    "download_failed": "the provider download failed",
    "yt_dlp_error": "the provider download failed validation",
    "source_validation_error": "the source failed a safety or identity check",
    "source_candidate_rejected": "the source contradicted the approved recording",
    "media_validation_error": "the downloaded media failed technical validation",
    "media_budget_exceeded": "the source exceeded the configured media limit",
    "inferred_version_revalidated": "an obsolete inferred version was revalidated",
    "probe_metadata_invalid": "the provider returned unusable source metadata",
    "probe_provider_rejected": "the provider could not validate this source",
    "probe_validation_rejected": "the source failed a safety validation check",
}
_SOURCE_DECISION_REASON_LABELS = {
    "prevalidated_direct_source": "selected from the validated direct source",
    "local_auto_match": "selected by deterministic source ranking",
    "ai_match_accepted": "selected after finite-candidate model adjudication",
    "source_contradiction": "source identity contradicted the approved recording",
    "duration_not_confirmed": "source duration could not be confirmed",
    "local_score_below_threshold": "local identity score was below the automatic threshold",
    "local_lead_too_small": "another materially different source scored too closely",
    "no_eligible_source": "no candidate satisfied the source policy",
    "inferred_version_revalidated": "an obsolete inferred version was replaced",
}
_SOURCE_POLICY_LABELS = {
    "pending": "policy pending",
    "allowed": "policy allowed",
    "rejected": "policy rejected",
    "exhausted": "attempt exhausted",
}
_SOURCE_PROBE_LABELS = {
    "pending": "probe pending",
    "valid": "probe valid",
    "invalid": "probe invalid",
    "failed": "probe failed",
}
_SOURCE_DISCOVERY_DIAGNOSTICS_KIND = "source_search_diagnostics"


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
        display
        for key in (
            "label",
            "artist",
            "title",
            "album",
            "channel",
            "source_id",
            "reason",
        )
        if (display := _bounded_display(payload.get(key))) is not None
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
        "provider": _provider_display(payload.get("provider") or payload.get("source_provider")),
        "uploader": _bounded_display(
            payload.get("uploader") or payload.get("channel") or payload.get("source_uploader")
        ),
        "uploader_relationship": _bounded_display(payload.get("uploader_relationship")),
        "duration_seconds": duration_seconds,
        "version": _bounded_display(payload.get("version") or payload.get("version_signature")),
        "album": _bounded_display(payload.get("album")),
        "year": _bounded_display(payload.get("year")),
        "release_status": _bounded_display(payload.get("release_status") or payload.get("status")),
        "primary_type": _bounded_display(payload.get("primary_type")),
    }


def _bounded_display(value: object, *, limit: int = 300) -> str | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = "".join(
        character for character in normalized if not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(normalized.split())
    return normalized[:limit] or None


def _bounded_diagnostic_text(value: object, *, limit: int = 300) -> str | None:
    display = _bounded_display(value, limit=limit)
    if display is not None and _DIAGNOSTIC_QUERY_SECRET.search(display):
        return "[redacted unsafe provider text]"
    return display


def _json_object(value: str | None) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_integer(value: object, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum:
        return value
    return None


def _safe_unit_score(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1:
        return float(value)
    return None


def _provider_display(value: object) -> str | None:
    return _PROVIDER_LABELS.get(value) if isinstance(value, str) else None


def _safe_execution_presentation(item: object) -> dict[str, object] | None:
    reason = getattr(item, "termination_reason", None)
    if not isinstance(reason, str):
        return None
    attempt = getattr(item, "orchestration_attempt_id", None)
    return {
        "reason": _TERMINATION_LABELS.get(reason, "Stopped for an unclassified safe reason"),
        "attempt_id": attempt
        if isinstance(attempt, str) and _ATTEMPT_ID.fullmatch(attempt)
        else None,
        "rounds_used": _safe_integer(
            getattr(item, "model_rounds_used", None), minimum=0, maximum=50
        ),
        "round_budget": _safe_integer(
            getattr(item, "configured_model_rounds", None), minimum=1, maximum=50
        ),
        "tool_cap": _safe_integer(
            getattr(item, "configured_tool_calls", None), minimum=1, maximum=50
        ),
        "deadline_seconds": _safe_integer(
            getattr(item, "configured_agent_seconds", None), minimum=10, maximum=600
        ),
    }


def _discovery_payloads(metadata: dict[str, object]) -> tuple[dict[str, object], ...]:
    """Return bounded diagnostic runs while accepting the legacy latest-only envelope."""

    result: list[dict[str, object]] = []
    raw_runs = metadata.get("discovery_diagnostic_runs")
    if isinstance(raw_runs, list):
        for raw in raw_runs[-10:]:
            if not isinstance(raw, dict):
                continue
            nested = raw.get("discovery_diagnostics")
            result.append(nested if isinstance(nested, dict) else raw)
    latest = metadata.get("discovery_diagnostics")
    if isinstance(latest, dict) and (not result or result[-1] != latest):
        result.append(latest)
    return tuple(result[-10:])


def _safe_discovery_lines(
    discovery: dict[str, object], *, include_queries: bool
) -> tuple[str, ...]:
    lines: list[str] = []
    count_specs = (
        ("query_variant_count", "query variants", 24),
        ("found_count", "found", 1_000),
        ("probed_count", "probed", 20),
        ("accepted_count", "accepted", 100),
    )
    counts = [
        f"{count} {label}"
        for key, label, maximum in count_specs
        if (count := _safe_integer(discovery.get(key), minimum=0, maximum=maximum)) is not None
    ]
    if counts:
        lines.append("Discovery: " + " · ".join(counts))
    rejection_counts = discovery.get("rejection_counts")
    if isinstance(rejection_counts, dict):
        rejected = [
            f"{count} {label}"
            for key, label in _DISCOVERY_REJECTION_LABELS.items()
            if (count := _safe_integer(rejection_counts.get(key), minimum=1, maximum=1_000))
            is not None
        ]
        if rejected:
            lines.append("Filtered: " + " · ".join(rejected))
    if discovery.get("stopped_early") is True:
        lines.append("Discovery stopped after reaching its bounded candidate target")
    query_attempts = discovery.get("query_attempts")
    if include_queries and isinstance(query_attempts, list):
        for attempt in query_attempts[:12]:
            if not isinstance(attempt, dict):
                continue
            provider = attempt.get("provider")
            query = _bounded_display(attempt.get("query"), limit=300)
            found = _safe_integer(attempt.get("found_count"), minimum=0, maximum=10)
            if (
                not isinstance(provider, str)
                or provider not in _DIAGNOSTIC_PROVIDERS
                or query is None
                or found is None
            ):
                continue
            safe_query = (
                "[redacted unsafe query]" if _DIAGNOSTIC_QUERY_SECRET.search(query) else query
            )
            lines.append(f"{_PROVIDER_LABELS[provider]} search: {safe_query} · {found} found")
    return tuple(lines[:16])


def _safe_source_diagnostics(
    source: SourceCandidate | EvidenceReference | None, *, include_queries: bool
) -> tuple[str, ...]:
    """Allowlist persisted operational facts; never return raw query/provider fields."""

    if source is None:
        return ()
    metadata = _json_object(source.sanitized_metadata_json)
    lines: list[str] = []
    for discovery in _discovery_payloads(metadata):
        for line in _safe_discovery_lines(discovery, include_queries=include_queries):
            if line not in lines:
                lines.append(line)

    ranking = metadata.get("ranking_facts")
    if isinstance(ranking, dict):
        components = [
            f"{label} {score:.0%}"
            for key, label in _RANKING_FACT_LABELS.items()
            if (score := _safe_unit_score(ranking.get(key))) is not None
        ]
        if components:
            lines.append("Ranking: " + " · ".join(components))
        checks: list[str] = []
        for key, label in (
            ("canonical_exact", "exact recording fields"),
            ("version_match", "version compatible"),
            ("duration_compatible", "duration compatible"),
        ):
            value = ranking.get(key)
            if isinstance(value, bool):
                checks.append(f"{label}: {'yes' if value else 'no'}")
        if checks:
            lines.append("Checks: " + " · ".join(checks))
        contradictions = ranking.get("contradiction_codes")
        if isinstance(contradictions, list):
            labels = [
                _SOURCE_CONTRADICTION_LABELS[value]
                for value in contradictions[:16]
                if isinstance(value, str) and value in _SOURCE_CONTRADICTION_LABELS
            ]
            if labels:
                lines.append("Conflicts: " + " · ".join(dict.fromkeys(labels)))
    return tuple(lines[:40])


def _safe_json_strings(value: str | None, *, limit: int = 16) -> tuple[str, ...]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed[:limit] if isinstance(item, str))


def _safe_source_failure(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _SOURCE_FAILURE_LABELS.get(value, "an unclassified source validation failed")


def _safe_source_decision_reasons(decision: JobDecision | None) -> tuple[str, ...]:
    if decision is None:
        return ()
    reasons: list[str] = []
    for code in _safe_json_strings(decision.reason_codes_json):
        label = _SOURCE_DECISION_REASON_LABELS.get(code)
        if label is None and code.startswith("source_failed:"):
            label = _safe_source_failure(code.removeprefix("source_failed:"))
        if label is not None:
            reasons.append(label)
    return tuple(dict.fromkeys(reasons))[:4]


def _source_candidate_diagnostic_view(
    candidate: SourceCandidate,
    *,
    active_source_candidate_id: str | None,
    decision: JobDecision | None,
) -> dict[str, object]:
    selected = candidate.id == active_source_candidate_id or (
        active_source_candidate_id is None and decision is not None and decision.state == "selected"
    )
    contradictions = tuple(
        {"code": code, "label": _SOURCE_CONTRADICTION_LABELS[code]}
        for code in dict.fromkeys(_safe_json_strings(candidate.contradictions_json))
        if code in _SOURCE_CONTRADICTION_LABELS
    )[:8]
    rejected = bool(
        not selected
        and (
            (decision is not None and decision.state == "rejected")
            or candidate.policy_status in {"rejected", "exhausted"}
            or candidate.probe_status in {"invalid", "failed"}
            or candidate.failure_code is not None
        )
    )
    status_label = (
        "Selected"
        if selected
        else "Rejected"
        if rejected
        else "Conflicting candidate"
        if contradictions
        else "Eligible alternative"
        if candidate.policy_status == "allowed" and candidate.probe_status == "valid"
        else "Pending validation"
    )
    rationale = list(_safe_source_decision_reasons(decision))
    failure = _safe_source_failure(candidate.failure_code)
    if failure is not None and failure not in rationale:
        rationale.append(failure)
    if not rationale:
        if selected:
            rationale.append("retained as the job's validated source")
        elif rejected and contradictions:
            rationale.append("rejected because the source conflicts with the approved recording")
        elif rejected:
            rationale.append("rejected by bounded source validation")
        elif contradictions:
            rationale.append("not selected because it conflicts with the approved recording")
        elif candidate.policy_status == "allowed" and candidate.probe_status == "valid":
            rationale.append("kept as a lower-ranked validated alternative")
        else:
            rationale.append("awaiting bounded source validation")
    all_diagnostics = _safe_source_diagnostics(candidate, include_queries=False)
    return {
        "provider": _provider_display(candidate.provider) or "Unknown provider",
        "title": _bounded_diagnostic_text(candidate.provider_title, limit=500),
        "duration_seconds": (
            float(candidate.duration_seconds)
            if isinstance(candidate.duration_seconds, int | float)
            and not isinstance(candidate.duration_seconds, bool)
            and 0 < candidate.duration_seconds <= 14_400
            else None
        ),
        "reference": _bounded_display(candidate.id, limit=36),
        "uploader": _bounded_display(candidate.uploader),
        "uploader_relationship": (
            _bounded_display(candidate.uploader_relationship, limit=24)
            if candidate.uploader_relationship
            in {
                "official_artist",
                "official_label",
                "topic",
                "distributor",
                "third_party",
                "unknown",
            }
            else "unknown"
        ),
        "status": status_label,
        "policy": _SOURCE_POLICY_LABELS.get(candidate.policy_status, "policy unknown"),
        "probe": _SOURCE_PROBE_LABELS.get(candidate.probe_status, "probe unknown"),
        "local_score": _safe_unit_score(candidate.local_score),
        "contradictions": contradictions,
        "rationale": tuple(rationale[:4]),
        "ranking": tuple(
            line for line in all_diagnostics if line.startswith(("Ranking:", "Checks:"))
        )[:2],
    }


def _source_resolution_diagnostic_view(
    candidates: list[SourceCandidate],
    *,
    active_source_candidate_id: str | None,
    decisions_by_candidate: dict[str, JobDecision],
    discovery_reference: EvidenceReference | None = None,
) -> dict[str, object] | None:
    if not candidates and discovery_reference is None:
        return None
    discovery_lines: list[str] = []
    diagnostic_sources: tuple[SourceCandidate | EvidenceReference, ...] = (
        *((discovery_reference,) if discovery_reference is not None else ()),
        *candidates,
    )
    for candidate in diagnostic_sources:
        for line in _safe_source_diagnostics(candidate, include_queries=True):
            if (
                line.startswith(
                    (
                        "Discovery:",
                        "Filtered:",
                        "Discovery stopped",
                        *_SOURCE_SEARCH_PREFIXES,
                    )
                )
                and line not in discovery_lines
            ):
                discovery_lines.append(line)
            if len(discovery_lines) >= 16:
                break
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.id != active_source_candidate_id,
            -candidate.local_score,
            candidate.id,
        ),
    )[:24]
    return {
        "discovery": tuple(discovery_lines[:16]),
        "candidates": tuple(
            _source_candidate_diagnostic_view(
                candidate,
                active_source_candidate_id=active_source_candidate_id,
                decision=decisions_by_candidate.get(candidate.id),
            )
            for candidate in ordered
        ),
    }


def _safe_track_presentation(
    track: object,
    source: SourceCandidate | None = None,
    *,
    include_admin_diagnostics: bool = False,
    discovery_references: tuple[EvidenceReference, ...] = (),
) -> dict[str, object]:
    """Build display-only match context without returning executable URLs or raw payloads."""

    provenance = _json_object(getattr(track, "metadata_provenance_json", None))
    raw_constraints = provenance.get("request_constraints")
    constraints = raw_constraints if isinstance(raw_constraints, dict) else {}
    canonical = bool(getattr(track, "canonical_identity_verified", False))
    version = _bounded_display(getattr(track, "version_signature", None)) or "studio"
    requested_version = _bounded_display(constraints.get("requested_version"))
    version_basis = (
        "explicit request constraint applied"
        if requested_version is not None
        else "verified canonical match"
        if canonical
        else "provisional until source verification"
    )
    album = _bounded_display(getattr(track, "album", None))
    year = getattr(track, "year", None)
    release_basis = (
        "selected canonical release"
        if canonical and getattr(track, "release_mbid", None)
        else "suggested release context"
        if album
        else None
    )
    source_score = source.local_score if source is not None else None
    confidence = (
        float(source_score)
        if isinstance(source_score, int | float)
        and not isinstance(source_score, bool)
        and 0 <= source_score <= 1
        else None
    )
    source_name = _provider_display(source.provider) if source is not None else None
    uploader = _bounded_display(source.uploader) if source is not None else None
    provenance_source = provenance.get("source")
    safe_sources = {
        "unverified_model_output": "proposal output; locally verified later",
        "musicbrainz_search_recordings": "local MusicBrainz candidate",
        "openai_canonical_match": "finite-candidate adjudication",
        "validated_direct_provider_metadata": "validated direct-provider metadata",
        "validated_direct_collection_metadata": "validated collection metadata",
    }
    return {
        "version": version,
        "requested_version": requested_version,
        "version_basis": version_basis,
        "album": album,
        "year": year if isinstance(year, int) and not isinstance(year, bool) else None,
        "release_basis": release_basis,
        "canonical": canonical,
        "source": source_name,
        "uploader": uploader,
        "source_confidence": confidence,
        "diagnostic_source": (
            safe_sources.get(provenance_source, "not yet resolved")
            if isinstance(provenance_source, str)
            else "not yet resolved"
        ),
        "source_diagnostics": (
            tuple(
                dict.fromkeys(
                    line
                    for diagnostic_source in (*discovery_references, source)
                    for line in _safe_source_diagnostics(diagnostic_source, include_queries=True)
                )
            )[:40]
            if include_admin_diagnostics
            else ()
        ),
    }


def _friendly_stage(value: str) -> str:
    return {
        "queued": "Waiting to start",
        "resolving_source": "Finding the best safe source",
        "waiting_ai": "Confirming the match",
        "downloading": "Downloading audio",
        "resolving_metadata": "Confirming metadata",
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
    track_ids = [track.id for track in tracks]
    source_by_track: dict[str, SourceCandidate] = {}
    discovery_by_track: dict[str, list[EvidenceReference]] = {
        track_id: [] for track_id in track_ids
    }
    if track_ids:
        single_track_id = track_ids[0] if len(track_ids) == 1 else None
        candidate_scope: ColumnElement[bool] = SourceCandidate.request_track_id.in_(track_ids)
        diagnostic_scope: ColumnElement[bool] = EvidenceReference.request_track_id.in_(track_ids)
        if single_track_id is not None:
            candidate_scope = or_(
                candidate_scope,
                (
                    SourceCandidate.request_track_id.is_(None)
                    & (EvidenceReference.request_id == request_id)
                    & EvidenceReference.request_track_id.is_(None)
                    & EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS)
                ),
            )
            diagnostic_scope = or_(
                diagnostic_scope,
                EvidenceReference.request_track_id.is_(None),
            )
        with request.app.state.session_factory() as session:
            candidates = list(
                session.scalars(
                    select(SourceCandidate)
                    .outerjoin(
                        EvidenceReference,
                        SourceCandidate.evidence_id == EvidenceReference.id,
                    )
                    .where(
                        candidate_scope,
                        SourceCandidate.policy_status == "allowed",
                        SourceCandidate.probe_status == "valid",
                    )
                    .order_by(
                        case((SourceCandidate.request_track_id.is_(None), 1), else_=0),
                        SourceCandidate.request_track_id,
                        SourceCandidate.local_score.desc(),
                        SourceCandidate.id,
                    )
                    .limit(min(6_000, max(1, len(track_ids) * 24)))
                )
            )
            diagnostic_references = (
                list(
                    session.scalars(
                        select(EvidenceReference)
                        .where(
                            EvidenceReference.request_id == request_id,
                            EvidenceReference.evidence_kind == _SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
                            diagnostic_scope,
                        )
                        .order_by(EvidenceReference.created_at, EvidenceReference.id)
                        .limit(min(250, max(1, len(track_ids) * 10)))
                    )
                )
                if authenticated.role == "admin"
                else []
            )
        for candidate in candidates:
            owner_track_id = candidate.request_track_id or single_track_id
            if owner_track_id is not None:
                source_by_track.setdefault(owner_track_id, candidate)
        for reference in diagnostic_references:
            owner_track_id = reference.request_track_id or single_track_id
            if owner_track_id in discovery_by_track:
                discovery_by_track[owner_track_id].append(reference)
    track_presentations = {
        track.id: _safe_track_presentation(
            track,
            source_by_track.get(track.id),
            include_admin_diagnostics=authenticated.role == "admin",
            discovery_references=tuple(discovery_by_track[track.id]),
        )
        for track in tracks
    }
    return _render(
        request,
        "request.html",
        _context(
            request,
            authenticated,
            event_cursor=event_cursor,
            item=item,
            tracks=tracks,
            track_presentations=track_presentations,
            execution_diagnostics=_safe_execution_presentation(item),
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
    job_by_track_id = {job.request_track_id: job.id for job in jobs}
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
                        JobDecision.state.in_(["pending", "selected", "rejected"]),
                    )
                    .order_by(
                        JobDecision.job_id,
                        JobDecision.category,
                        JobDecision.revision.desc(),
                    )
                    .limit(min(6_400, max(1, len(job_ids) * 64)))
                )
            )
            if job_ids
            else []
        )
        candidate_ranks = (
            select(
                SourceCandidate.id.label("candidate_id"),
                DownloadJob.id.label("owner_job_id"),
                func.row_number()
                .over(
                    partition_by=DownloadJob.id,
                    order_by=(
                        case(
                            (SourceCandidate.id == DownloadJob.active_source_candidate_id, 0),
                            else_=1,
                        ),
                        SourceCandidate.local_score.desc(),
                        SourceCandidate.id,
                    ),
                )
                .label("candidate_rank"),
            )
            .select_from(DownloadJob)
            .join(
                SourceCandidate,
                or_(
                    SourceCandidate.job_id == DownloadJob.id,
                    SourceCandidate.request_track_id == DownloadJob.request_track_id,
                ),
            )
            .where(DownloadJob.id.in_(job_ids))
            .subquery()
            if job_ids
            else None
        )
        source_candidates = (
            list(
                session.scalars(
                    select(SourceCandidate)
                    .join(
                        candidate_ranks,
                        candidate_ranks.c.candidate_id == SourceCandidate.id,
                    )
                    .where(candidate_ranks.c.candidate_rank <= 24)
                    .order_by(
                        candidate_ranks.c.owner_job_id,
                        candidate_ranks.c.candidate_rank,
                    )
                )
            )
            if candidate_ranks is not None
            else []
        )
        discovery_references = (
            list(
                session.scalars(
                    select(EvidenceReference)
                    .where(
                        EvidenceReference.job_id.in_(job_ids),
                        EvidenceReference.evidence_kind == _SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
                    )
                    .order_by(
                        EvidenceReference.job_id,
                        EvidenceReference.created_at,
                        EvidenceReference.id,
                    )
                    .limit(min(1_000, max(1, len(job_ids) * 10)))
                )
            )
            if job_ids and authenticated.role == "admin"
            else []
        )
    source_by_id = {candidate.id: candidate for candidate in source_candidates}
    sources_by_job: dict[str, list[SourceCandidate]] = {job_id: [] for job_id in job_ids}
    for candidate in source_candidates:
        owner_job_id = (
            candidate.job_id
            if candidate.job_id in sources_by_job
            else job_by_track_id.get(candidate.request_track_id)
        )
        if owner_job_id is not None and len(sources_by_job[owner_job_id]) < 24:
            sources_by_job[owner_job_id].append(candidate)
    discovery_by_job: dict[str, EvidenceReference] = {}
    for reference in discovery_references:
        if reference.job_id in sources_by_job:
            discovery_by_job[reference.job_id] = reference
    selected_sources: dict[str, dict[str, object]] = {
        job.id: {
            "provider": _provider_display(source_by_id[job.active_source_candidate_id].provider),
            "uploader": _bounded_display(source_by_id[job.active_source_candidate_id].uploader),
        }
        for job in jobs
        if job.active_source_candidate_id in source_by_id
    }
    pending_decision_ids = {decision.id for decision in decisions if decision.state == "pending"}
    source_decisions_by_job: dict[str, dict[str, JobDecision]] = {job_id: {} for job_id in job_ids}
    for decision in decisions:
        if decision.category != "acquisition_source" or decision.state == "pending":
            continue
        source_candidate_id = _json_object(decision.selected_payload_json).get(
            "source_candidate_id"
        )
        if isinstance(source_candidate_id, str):
            source_decisions_by_job[decision.job_id].setdefault(source_candidate_id, decision)
    source_resolution_diagnostics = (
        {
            job.id: diagnostic
            for job in jobs
            if (
                diagnostic := _source_resolution_diagnostic_view(
                    sources_by_job[job.id],
                    active_source_candidate_id=job.active_source_candidate_id,
                    decisions_by_candidate=source_decisions_by_job[job.id],
                    discovery_reference=discovery_by_job.get(job.id),
                )
            )
            is not None
        }
        if authenticated.role == "admin"
        else {}
    )
    for job in jobs:
        try:
            raw_snapshot = json.loads(job.approved_snapshot_json)
        except (TypeError, json.JSONDecodeError):
            raw_snapshot = {}
        snapshot = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        snapshots[job.id] = {
            "artist": _bounded_display(snapshot.get("artist")),
            "title": _bounded_display(snapshot.get("title")),
            "album": _bounded_display(snapshot.get("album")),
            "year": _safe_integer(snapshot.get("year"), minimum=1000, maximum=2999),
            "recording_mbid": _bounded_display(snapshot.get("recording_mbid"), limit=36),
            "release_mbid": _bounded_display(snapshot.get("release_mbid"), limit=36),
            "version_signature": _bounded_display(snapshot.get("version_signature")),
            "requested_version": _bounded_display(snapshot.get("requested_version")),
            "canonical_identity_verified": snapshot.get("canonical_identity_verified") is True,
        }
        try:
            raw_warnings = json.loads(job.warnings_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_warnings = []
        safe_warnings: list[dict[str, str]] = []
        for warning in raw_warnings:
            if not isinstance(warning, dict):
                continue
            message = _bounded_display(warning.get("message"), limit=500)
            if message is None:
                continue
            safe_warnings.append(
                {
                    "code": _bounded_display(warning.get("code"), limit=80) or "warning",
                    "message": message,
                }
            )
        warnings[job.id] = safe_warnings[:20]
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
                "label": decision.category.replace("_", " ").title(),
                "decided_by": (
                    decision.decided_by
                    if decision.decided_by in {"deterministic", "openai", "user", "migration"}
                    else "unknown"
                ),
                "confidence": _safe_unit_score(
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
                            "provider": _provider_display(source.provider),
                            "uploader": _bounded_display(source.uploader),
                            "uploader_relationship": _bounded_display(
                                source.uploader_relationship, limit=24
                            ),
                            "duration_seconds": (
                                float(source.duration_seconds)
                                if isinstance(source.duration_seconds, int | float)
                                and not isinstance(source.duration_seconds, bool)
                                and 0 < source.duration_seconds <= 14_400
                                else None
                            ),
                            "version": _bounded_display(source.version_signature),
                        }
                    )
                    detail["label"] = "Acquisition source"
            elif decision.category == "canonical_metadata":
                detail.update(
                    {
                        "artist": _bounded_display(payload.get("artist")),
                        "title": _bounded_display(payload.get("title")),
                        "album": _bounded_display(payload.get("album")),
                        "year": _safe_integer(payload.get("year"), minimum=1000, maximum=2999),
                        "version": _bounded_display(
                            payload.get("version") or payload.get("version_signature")
                        ),
                    }
                )
                detail["label"] = (
                    "Validated source metadata"
                    if payload.get("metadata_authority")
                    in {
                        "validated_provider",
                        "direct_user_source",
                        "user_confirmed_provider_metadata",
                    }
                    else "Canonical MusicBrainz match"
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
            selected_sources=selected_sources,
            source_resolution_diagnostics=source_resolution_diagnostics,
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
        "Canonical metadata policy": settings.canonical_metadata_policy,
        "Automatic provider metadata identity floor": (
            f"{settings.provider_metadata_fallback_min_score:.0%}"
        ),
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
