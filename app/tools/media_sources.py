from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.openai import strict_json_schema
from app.db.models import EvidenceReference, Request, RequestTrack, ServiceTask
from app.schemas import StrictModel
from app.sources import EXECUTABLE_EVIDENCE_KINDS
from app.tools.registry import ToolDefinition

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
MediaProvider = Literal["youtube", "soundcloud", "bandcamp"]
_MEDIA_PROVIDER_ORDER: tuple[MediaProvider, ...] = ("bandcamp", "soundcloud", "youtube")


@dataclass(frozen=True, slots=True)
class MediaToolAuthorization:
    user_id: str
    request_id: str
    requested_album: str | None = None
    requested_version: str | None = None


_MEDIA_TOOL_AUTHORIZATION: ContextVar[MediaToolAuthorization | None] = ContextVar(
    "music_agent_media_tool_authorization",
    default=None,
)


@contextmanager
def media_tool_authorization(
    user_id: str,
    request_id: str,
    *,
    requested_album: str | None = None,
    requested_version: str | None = None,
) -> Iterator[None]:
    """Bind read-only tools to one request and its trusted explicit constraints."""

    authorization = MediaToolAuthorization(
        user_id=user_id,
        request_id=request_id,
        requested_album=requested_album,
        requested_version=requested_version,
    )
    token = _MEDIA_TOOL_AUTHORIZATION.set(authorization)
    try:
        yield
    finally:
        _MEDIA_TOOL_AUTHORIZATION.reset(token)


def current_tool_authorization() -> MediaToolAuthorization | None:
    """Return request-scoped trusted context without accepting model-supplied flags."""

    return _MEDIA_TOOL_AUTHORIZATION.get()


class SearchMediaSourcesArguments(StrictModel):
    intent_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)
    provider: MediaProvider
    limit: int = Field(ge=1, le=10)


class ProbeMediaSourceArguments(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)


class _SanitizedSearchCandidate(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)
    provider: MediaProvider
    title: str = Field(min_length=1, max_length=500)
    uploader: str | None = Field(max_length=300)
    duration_seconds: float | None = Field(gt=0, le=14_400)


class _SanitizedSearchResult(StrictModel):
    intent_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)
    provider: MediaProvider
    candidates: list[_SanitizedSearchCandidate] = Field(max_length=10)


class _SanitizedProbeResult(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)
    source_candidate_id: str = Field(min_length=1, max_length=200, pattern=_OPAQUE_ID.pattern)
    provider: MediaProvider
    title: str = Field(min_length=1, max_length=500)
    provider_artist: str | None = Field(max_length=300)
    uploader: str | None = Field(max_length=300)
    duration_seconds: float | None = Field(gt=0, le=14_400)
    uploader_relationship: Literal[
        "official_artist",
        "official_label",
        "topic",
        "distributor",
        "third_party",
        "unknown",
    ]
    version_signature: str = Field(min_length=1, max_length=300)


def build_media_source_tools(
    session_factory: sessionmaker[Session],
    *,
    broker_timeout_seconds: float = 55.0,
    enabled_providers: Iterable[str] = _MEDIA_PROVIDER_ORDER,
) -> tuple[ToolDefinition, ToolDefinition]:
    """Build the two finite, database-brokered media discovery tools.

    The web process never receives an executable URL. The worker stores provider
    URLs and returns only opaque database identifiers plus bounded display data.
    """

    enabled = tuple(dict.fromkeys(value.casefold() for value in enabled_providers))
    unknown = sorted(set(enabled) - set(_MEDIA_PROVIDER_ORDER))
    if unknown:
        raise ValueError(f"unsupported enabled media provider: {unknown[0]}")
    if not enabled:
        raise ValueError("at least one media provider must be enabled")
    enabled_set = frozenset(enabled)

    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
        values = SearchMediaSourcesArguments.model_validate(arguments)
        if values.provider not in enabled_set:
            raise ValueError("media provider is disabled")
        await asyncio.to_thread(_require_intent, session_factory, values.intent_id)
        payload = await _run_worker_task(
            session_factory,
            kind="search_media_sources",
            payload=values.model_dump(mode="json"),
            timeout_seconds=broker_timeout_seconds,
        )
        return _SanitizedSearchResult.model_validate(payload).model_dump(mode="json")

    async def probe(arguments: dict[str, Any]) -> dict[str, Any]:
        values = ProbeMediaSourceArguments.model_validate(arguments)
        await asyncio.to_thread(_require_evidence, session_factory, values.evidence_id)
        payload = await _run_worker_task(
            session_factory,
            kind="probe_media_source",
            payload=values.model_dump(mode="json"),
            timeout_seconds=broker_timeout_seconds,
        )
        return _SanitizedProbeResult.model_validate(payload).model_dump(mode="json")

    search_parameters = strict_json_schema(SearchMediaSourcesArguments.model_json_schema())
    search_parameters["properties"]["provider"]["enum"] = [
        provider for provider in _MEDIA_PROVIDER_ORDER if provider in enabled_set
    ]
    search_tool = ToolDefinition(
        name="search_media_sources",
        description=(
            "Search one enabled curated provider for finite media evidence tied to the supplied "
            "request intent ID. Returns opaque evidence IDs and bounded untrusted provider "
            "metadata, never executable URLs."
        ),
        parameters=search_parameters,
        handler=search,
        timeout_seconds=broker_timeout_seconds + 2,
        max_result_bytes=32_000,
        # Authorization is request-scoped; a global tool cache could otherwise
        # return a prior request's opaque evidence before the handler rechecks it.
        cache_ttl_seconds=None,
    )
    probe_tool = ToolDefinition(
        name="probe_media_source",
        description=(
            "Probe one opaque evidence ID previously returned by media search. Returns an opaque "
            "finite source-candidate ID and sanitized metadata, never an executable URL."
        ),
        parameters=strict_json_schema(ProbeMediaSourceArguments.model_json_schema()),
        handler=probe,
        timeout_seconds=broker_timeout_seconds + 2,
        max_result_bytes=16_000,
        cache_ttl_seconds=None,
    )
    return search_tool, probe_tool


async def _run_worker_task(
    session_factory: sessionmaker[Session],
    *,
    kind: str,
    payload: dict[str, object],
    timeout_seconds: float,
) -> dict[str, Any]:
    def enqueue() -> str:
        with session_factory.begin() as session:
            task = ServiceTask(
                target="worker",
                kind=kind,
                payload_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                available_at=datetime.now(UTC),
            )
            session.add(task)
            session.flush()
            return task.id

    task_id = await asyncio.to_thread(enqueue)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        state, result_json, last_error = await asyncio.to_thread(
            _read_task,
            session_factory,
            task_id,
        )
        if state == "completed":
            if result_json is None:
                raise RuntimeError("media worker task completed without a result")
            result = json.loads(result_json)
            if not isinstance(result, dict):
                raise RuntimeError("media worker task returned an invalid result")
            return result
        if state == "failed":
            # RuntimeError is deliberately rendered as a generic provider error by
            # ToolRegistry so a worker diagnostic cannot leak a stored URL.
            raise RuntimeError(last_error or "media worker task failed")
        await asyncio.sleep(0.2)
    raise TimeoutError("media worker task timed out")


def _read_task(
    session_factory: sessionmaker[Session],
    task_id: str,
) -> tuple[str, str | None, str | None]:
    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        if task is None:
            raise RuntimeError("media worker task disappeared")
        return task.state, task.result_json, task.last_error


def _require_intent(session_factory: sessionmaker[Session], intent_id: str) -> None:
    authorization = _require_authorization()
    with session_factory() as session:
        request = session.scalar(
            select(Request).where(
                Request.id == authorization.request_id,
                Request.user_id == authorization.user_id,
            )
        )
        if request is not None:
            if intent_id == request.id:
                return
            track = session.scalar(
                select(RequestTrack.id).where(
                    RequestTrack.id == intent_id,
                    RequestTrack.request_id == request.id,
                )
            )
            if track is not None:
                return
    raise ValueError("intent_id does not identify an active local request")


def _require_evidence(session_factory: sessionmaker[Session], evidence_id: str) -> None:
    authorization = _require_authorization()
    with session_factory() as session:
        evidence = session.scalar(
            select(EvidenceReference.id)
            .join(Request, Request.id == EvidenceReference.request_id)
            .where(
                EvidenceReference.id == evidence_id,
                EvidenceReference.request_id == authorization.request_id,
                Request.user_id == authorization.user_id,
                EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                EvidenceReference.status.in_(["pending", "available"]),
                EvidenceReference.canonical_url.is_not(None),
            )
        )
    if evidence is None:
        raise ValueError("evidence_id is unknown, unavailable, or expired")


def _require_authorization() -> MediaToolAuthorization:
    authorization = _MEDIA_TOOL_AUTHORIZATION.get()
    if authorization is None:
        raise ValueError("media tool authorization context is missing")
    return authorization
