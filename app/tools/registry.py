from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.clients.apple_metadata import AppleMetadataClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.openai import strict_json_schema
from app.config import Settings
from app.repositories.cache import ExternalCacheRepository

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
CacheVary = Callable[[], Any]
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DEFAULT_TOOL_NAMES = frozenset(
    {
        "search_library",
        "get_library_summary",
        "musicbrainz_search_recordings",
        "musicbrainz_search_releases",
        "listenbrainz_popular_recordings",
        "listenbrainz_artist_radio",
        "listenbrainz_user_recommendations",
    }
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]
    handler: ToolHandler
    read_only: bool = True
    timeout_seconds: float = 25.0
    max_result_bytes: int = 96_000
    cache_ttl_seconds: int | None = None
    cache_vary: CacheVary | None = None

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": strict_json_schema(self.parameters),
            "strict": True,
        }


@dataclass(frozen=True, slots=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    output: str
    summary: dict[str, Any]
    duration_ms: int
    status: str


class ToolRegistry:
    """Allowlisted registry for bounded, read-only model tools."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._session_factory = session_factory
        self._closers: list[Callable[[], Awaitable[None] | None]] = []

    def register(self, definition: ToolDefinition) -> None:
        if not _TOOL_NAME.fullmatch(definition.name):
            raise ValueError(f"invalid tool name: {definition.name}")
        if not definition.read_only:
            raise ValueError("model tools must be read-only")
        if definition.name in self._definitions:
            raise ValueError(f"duplicate tool: {definition.name}")
        if definition.timeout_seconds <= 0 or definition.max_result_bytes < 1024:
            raise ValueError("tool limits must be positive")
        self._definitions[definition.name] = definition

    def add_closer(self, closer: Callable[[], Awaitable[None] | None]) -> None:
        self._closers.append(closer)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        return [definition.openai_schema() for definition in self._definitions.values()]

    async def execute(self, name: str, arguments_json: str) -> ToolExecution:
        started = time.monotonic()
        definition = self._definitions.get(name)
        if definition is None:
            return _error_execution(
                name,
                {},
                started,
                "unknown_tool",
                "The requested tool is not available.",
            )
        try:
            arguments = json.loads(arguments_json)
        except (TypeError, json.JSONDecodeError):
            return _error_execution(
                name, {}, started, "invalid_arguments", "Tool arguments must be a JSON object."
            )
        if not isinstance(arguments, dict):
            return _error_execution(
                name, {}, started, "invalid_arguments", "Tool arguments must be a JSON object."
            )
        if len(arguments_json.encode("utf-8")) > 32_000:
            return _error_execution(
                name,
                {},
                started,
                "invalid_arguments",
                "Tool arguments exceed the configured size limit.",
            )

        cached = self._cache_get(definition, arguments)
        if cached is not None:
            return _success_execution(name, arguments, cached, started, definition, cached=True)
        try:
            result = await asyncio.wait_for(
                definition.handler(arguments), timeout=definition.timeout_seconds
            )
            encoded = _encode_result(result)
            if len(encoded.encode("utf-8")) > definition.max_result_bytes:
                return _error_execution(
                    name,
                    arguments,
                    started,
                    "result_too_large",
                    "The provider result exceeded the configured bound; narrow the query.",
                )
            self._cache_put(definition, arguments, result)
            return _success_execution(name, arguments, result, started, definition)
        except TimeoutError:
            return _error_execution(
                name, arguments, started, "timeout", "The provider did not respond in time."
            )
        except (ValueError, LookupError) as error:
            return _error_execution(
                name, arguments, started, "invalid_arguments", _safe_error_message(error)
            )
        except Exception as error:  # provider and validation failures are model-visible, not fatal
            return _error_execution(
                name, arguments, started, "provider_error", _safe_error_message(error)
            )

    async def aclose(self) -> None:
        closers = tuple(reversed(self._closers))
        self._closers.clear()
        first_error: BaseException | None = None
        for closer in closers:
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _cache_get(self, definition: ToolDefinition, arguments: Mapping[str, Any]) -> Any | None:
        if definition.cache_ttl_seconds is None or self._session_factory is None:
            return None
        try:
            with self._session_factory.begin() as session:
                entry = ExternalCacheRepository(session).get(
                    f"tool:{definition.name}",
                    _cache_key(
                        arguments,
                        vary=definition.cache_vary() if definition.cache_vary else None,
                    ),
                )
                return entry.payload if entry is not None else None
        except Exception:
            return None

    def _cache_put(
        self,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
        result: Any,
    ) -> None:
        if definition.cache_ttl_seconds is None or self._session_factory is None:
            return
        try:
            with self._session_factory.begin() as session:
                ExternalCacheRepository(session).put(
                    f"tool:{definition.name}",
                    _cache_key(
                        arguments,
                        vary=definition.cache_vary() if definition.cache_vary else None,
                    ),
                    result,
                    ttl=timedelta(seconds=definition.cache_ttl_seconds),
                )
        except Exception:
            return


def build_default_registry(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    musicbrainz_client: MusicBrainzClient | None = None,
    listenbrainz_client: ListenBrainzClient | None = None,
    apple_client: AppleMetadataClient | None = None,
    media_source_tools: Sequence[ToolDefinition] = (),
    youtube_search_tool: ToolDefinition | None = None,
) -> ToolRegistry:
    """Build the fixed production allowlist without making network requests."""

    from app.tools.library import register_library_tools
    from app.tools.listenbrainz import register_listenbrainz_tools
    from app.tools.musicbrainz import register_musicbrainz_tools

    registry = ToolRegistry(session_factory)
    register_library_tools(registry, session_factory)

    owns_musicbrainz = musicbrainz_client is None
    musicbrainz = musicbrainz_client or MusicBrainzClient(settings)
    owns_apple = apple_client is None
    apple = apple_client or AppleMetadataClient(settings)
    register_musicbrainz_tools(
        registry, musicbrainz, apple if settings.apple_metadata_enabled else None
    )
    if owns_musicbrainz:
        registry.add_closer(musicbrainz.aclose)
    if owns_apple:
        registry.add_closer(apple.aclose)

    owns_listenbrainz = listenbrainz_client is None
    listenbrainz = listenbrainz_client or ListenBrainzClient(settings)
    register_listenbrainz_tools(
        registry,
        listenbrainz,
        default_username=settings.listenbrainz_username,
    )
    if owns_listenbrainz:
        registry.add_closer(listenbrainz.aclose)
    if frozenset(definition.name for definition in registry.definitions) != _DEFAULT_TOOL_NAMES:
        raise RuntimeError("default model tool allowlist is incomplete")
    if media_source_tools:
        names = tuple(definition.name for definition in media_source_tools)
        if len(names) != 2 or frozenset(names) != {
            "search_media_sources",
            "probe_media_source",
        }:
            raise ValueError(
                "media source broker must provide search_media_sources and probe_media_source"
            )
        for definition in media_source_tools:
            registry.register(definition)
    if youtube_search_tool is not None:
        if media_source_tools:
            raise ValueError("legacy YouTube and finite media broker tools cannot be combined")
        if youtube_search_tool.name != "youtube_search_candidates":
            raise ValueError("worker broker tool must be named youtube_search_candidates")
        registry.register(youtube_search_tool)
    return registry


def _success_execution(
    name: str,
    arguments: dict[str, Any],
    result: Any,
    started: float,
    definition: ToolDefinition,
    *,
    cached: bool = False,
) -> ToolExecution:
    output = _encode_result({"ok": True, "cached": cached, "result": result})
    return ToolExecution(
        name=name,
        arguments=arguments,
        output=output,
        summary={
            "ok": True,
            "cached": cached,
            "bytes": len(output.encode("utf-8")),
            "items": _item_count(result),
            "limit_bytes": definition.max_result_bytes,
        },
        duration_ms=_elapsed_ms(started),
        status="completed",
    )


def _error_execution(
    name: str,
    arguments: dict[str, Any],
    started: float,
    code: str,
    message: str,
) -> ToolExecution:
    result = {"ok": False, "error": {"code": code, "message": message[:300]}}
    output = _encode_result(result)
    return ToolExecution(
        name=name,
        arguments=arguments,
        output=output,
        summary={"ok": False, "error_code": code, "bytes": len(output.encode("utf-8"))},
        duration_ms=_elapsed_ms(started),
        status="failed",
    )


def _encode_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _cache_key(arguments: Mapping[str, Any], *, vary: Any = None) -> str:
    payload = _encode_result({"arguments": arguments, "trusted_context": vary})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _item_count(value: Any) -> int | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    if isinstance(value, Mapping):
        for key in ("items", "tracks", "recordings", "artists", "releases", "release-groups"):
            child = value.get(key)
            if isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                return len(child)
    return None


def _safe_error_message(error: Exception) -> str:
    # Provider exceptions can contain URLs, headers, or response bodies. Only
    # validation-style built-ins are safe to echo; other types get a generic label.
    if isinstance(error, (ValueError, LookupError)):
        return str(error)[:300] or "Invalid tool arguments."
    return f"{type(error).__name__}: external provider request failed"
