from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from app.clients.openai import strict_json_schema
from app.clients.ytdlp import (
    CancellationSignal,
    SourceValidationError,
    YtDlpClient,
    validate_public_media_metadata,
)
from app.db.models import ServiceTask
from app.schemas import StrictModel
from app.services.source_selection import (
    SelectionDecision,
    SourceCandidate,
    TrackIntent,
    select_source,
)
from app.tools.registry import ToolDefinition

_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True, slots=True)
class YouTubeSearchResponse:
    query: str
    candidates: tuple[SourceCandidate, ...]


class YouTubeSearchArguments(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    limit: int = Field(ge=1, le=8)


class YouTubeTool:
    """Narrow, policy-enforcing façade over yt-dlp's YouTube extractors."""

    def __init__(self, client: YtDlpClient, *, max_duration_seconds: int = 1800) -> None:
        if max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        self.client = client
        self.max_duration_seconds = max_duration_seconds

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        cancel_signal: CancellationSignal | None = None,
    ) -> YouTubeSearchResponse:
        payload = self.client.search(query, limit=limit, cancel_signal=cancel_signal)
        candidates: list[SourceCandidate] = []
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raw_entries = []
        for entry in raw_entries:
            candidate = _candidate_from_entry(entry, self.max_duration_seconds)
            if candidate is not None:
                candidates.append(candidate)
        return YouTubeSearchResponse(query=query, candidates=tuple(candidates))

    def choose(
        self,
        intent: TrackIntent,
        *,
        limit: int = 8,
        automatic_threshold: float = 0.84,
        cancel_signal: CancellationSignal | None = None,
    ) -> SelectionDecision:
        query = f"{intent.artist} {intent.title}"
        response = self.search(query, limit=limit, cancel_signal=cancel_signal)
        return select_source(
            intent,
            response.candidates,
            max_duration_seconds=self.max_duration_seconds,
            automatic_threshold=automatic_threshold,
        )

    def validate_direct_url(self, url: str) -> str:
        return self.client.validate_url(url)


def search_youtube(
    client: YtDlpClient,
    query: str,
    *,
    limit: int = 8,
    max_duration_seconds: int = 1800,
) -> tuple[SourceCandidate, ...]:
    return (
        YouTubeTool(client, max_duration_seconds=max_duration_seconds)
        .search(query, limit=limit)
        .candidates
    )


def build_youtube_search_tool(
    session_factory: sessionmaker[Session],
    *,
    broker_timeout_seconds: float = 55.0,
) -> ToolDefinition:
    """Create the fixed web-to-worker broker used by model orchestration."""

    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
        values = YouTubeSearchArguments.model_validate(arguments)

        def enqueue() -> str:
            with session_factory.begin() as session:
                task = ServiceTask(
                    target="worker",
                    kind="youtube_search",
                    payload_json=json.dumps(
                        {"query": values.query, "limit": values.limit},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    available_at=datetime.now(UTC),
                )
                session.add(task)
                session.flush()
                return task.id

        task_id = await asyncio.to_thread(enqueue)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + broker_timeout_seconds
        while loop.time() < deadline:

            def read_result() -> tuple[str, str | None, str | None]:
                with session_factory() as session:
                    task = session.get(ServiceTask, task_id)
                    if task is None:
                        raise RuntimeError("YouTube worker task disappeared")
                    return task.state, task.result_json, task.last_error

            state, result_json, last_error = await asyncio.to_thread(read_result)
            if state == "completed":
                if result_json is None:
                    raise RuntimeError("YouTube worker task completed without a result")
                payload = json.loads(result_json)
                if not isinstance(payload, dict):
                    raise RuntimeError("YouTube worker task returned an invalid result")
                return payload
            if state == "failed":
                raise RuntimeError(last_error or "YouTube worker task failed")
            await asyncio.sleep(0.2)
        raise TimeoutError("YouTube worker search timed out")

    return ToolDefinition(
        name="youtube_search_candidates",
        description=(
            "Search YouTube for at most eight bounded source candidates. Returns provider IDs, "
            "titles, channels, and durations; it does not download media."
        ),
        parameters=strict_json_schema(YouTubeSearchArguments.model_json_schema()),
        handler=search,
        timeout_seconds=broker_timeout_seconds + 2,
        max_result_bytes=32_000,
        cache_ttl_seconds=300,
    )


def _candidate_from_entry(value: object, max_duration_seconds: int) -> SourceCandidate | None:
    if not isinstance(value, dict):
        return None
    try:
        validate_public_media_metadata(value)
    except SourceValidationError:
        return None
    source_id = value.get("id")
    title = value.get("title")
    if not isinstance(source_id, str) or not _YOUTUBE_ID_RE.fullmatch(source_id):
        return None
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        return None
    extractor = value.get("extractor") or value.get("extractor_key") or value.get("ie_key")
    if extractor is not None and "youtube" not in str(extractor).casefold():
        return None
    duration = _duration(value.get("duration"))
    if duration is not None and duration > max_duration_seconds:
        return None
    channel = _first_string(value, "channel", "uploader", "channel_name")
    return SourceCandidate(
        source_id=source_id,
        url=f"https://www.youtube.com/watch?v={source_id}",
        title=title.strip(),
        channel=channel,
        duration_seconds=duration,
        extractor="youtube",
    )


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:300]
    return None


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if result <= 0:
        return None
    return result
