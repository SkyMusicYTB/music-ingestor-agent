from __future__ import annotations

import asyncio
import fcntl
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings

MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2/"
_RATE_LIMIT_FILENAME = ".musicbrainz-rate-limit"
SEARCH_ENTITIES = frozenset(
    {
        "area",
        "artist",
        "event",
        "instrument",
        "label",
        "place",
        "recording",
        "release",
        "release-group",
        "series",
        "work",
        "url",
    }
)
LOOKUP_ENTITIES = SEARCH_ENTITIES | {"collection", "genre"}
INCLUDES = frozenset(
    {
        "aliases",
        "annotation",
        "artist-credits",
        "artist-rels",
        "collections",
        "discids",
        "genres",
        "isrcs",
        "labels",
        "media",
        "ratings",
        "recording-level-rels",
        "recording-rels",
        "recordings",
        "release-group-level-rels",
        "release-group-rels",
        "release-groups",
        "release-rels",
        "releases",
        "tags",
        "url-rels",
        "work-level-rels",
        "work-rels",
        "works",
    }
)


class MusicBrainzError(RuntimeError):
    pass


class MusicBrainzNotFound(MusicBrainzError):
    pass


class MusicBrainzClient:
    """Rate-limited MusicBrainz WS/2 JSON client.

    MusicBrainz requires a meaningful User-Agent and no more than one request per
    second. The lock covers both waiting and sending so concurrent application
    tasks cannot accidentally exceed the shared client budget.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        rate_limit_path: Path | None = None,
        request_interval: float = 1.0,
        max_retries: int = 2,
    ) -> None:
        user_agent = settings.musicbrainz_user_agent.strip()
        if not user_agent:
            raise ValueError("MusicBrainz requires a non-empty User-Agent")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=MUSICBRAINZ_BASE_URL,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
        )
        # An injected client still receives the mandatory identification header.
        self._client.headers["User-Agent"] = user_agent
        self._client.headers["Accept"] = "application/json"
        self._sleep = sleep
        self._monotonic = monotonic
        self._request_interval = max(1.0, request_interval)
        state_directory = settings.database_path.expanduser().resolve(strict=False).parent
        self._host_rate_limiter = _HostRateLimiter(
            rate_limit_path or state_directory / _RATE_LIMIT_FILENAME,
            interval=self._request_interval,
            sleep=sleep,
            wall_clock=wall_clock,
        )
        self._max_retries = max(0, max_retries)
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> MusicBrainzClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        entity: str,
        query: str,
        *,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        entity = _entity(entity, SEARCH_ENTITIES)
        query = query.strip()
        if not query:
            raise ValueError("MusicBrainz search query cannot be empty")
        return await self._get_json(
            entity,
            params={
                "query": query,
                "limit": _bounded(limit, 1, 100, "limit"),
                "offset": _bounded(offset, 0, 100_000, "offset"),
                "fmt": "json",
            },
        )

    async def lookup(
        self,
        entity: str,
        mbid: str,
        *,
        includes: Sequence[str] = (),
    ) -> dict[str, Any] | None:
        entity = _entity(entity, LOOKUP_ENTITIES)
        mbid = _mbid(mbid)
        params: dict[str, str] = {"fmt": "json"}
        normalized_includes = _includes(includes)
        if normalized_includes:
            params["inc"] = "+".join(normalized_includes)
        try:
            return await self._get_json(f"{entity}/{mbid}", params=params)
        except MusicBrainzNotFound:
            return None

    async def browse(
        self,
        entity: str,
        linked_entity: str,
        linked_mbid: str,
        *,
        limit: int = 25,
        offset: int = 0,
        includes: Sequence[str] = (),
        filters: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        entity = _entity(entity, LOOKUP_ENTITIES)
        linked_entity = _entity(linked_entity.replace("_", "-"), LOOKUP_ENTITIES)
        params: dict[str, Any] = {
            linked_entity: _mbid(linked_mbid),
            "limit": _bounded(limit, 1, 100, "limit"),
            "offset": _bounded(offset, 0, 100_000, "offset"),
            "fmt": "json",
        }
        normalized_includes = _includes(includes)
        if normalized_includes:
            params["inc"] = "+".join(normalized_includes)
        for key, value in (filters or {}).items():
            if key not in {"type", "status", "release-group-status"}:
                raise ValueError(f"unsupported MusicBrainz browse filter: {key}")
            params[key] = value
        return await self._get_json(entity, params=params)

    async def search_recordings(
        self,
        *,
        artist: str,
        title: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        query = f'recording:"{_lucene_phrase(title)}" AND artist:"{_lucene_phrase(artist)}"'
        return await self.search("recording", query, limit=limit)

    async def search_artists(self, name: str, *, limit: int = 10) -> dict[str, Any]:
        return await self.search("artist", f'artist:"{_lucene_phrase(name)}"', limit=limit)

    async def search_release_groups(
        self,
        *,
        artist: str,
        title: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        query = f'releasegroup:"{_lucene_phrase(title)}" AND artist:"{_lucene_phrase(artist)}"'
        return await self.search("release-group", query, limit=limit)

    async def _get_json(self, path: str, *, params: Mapping[str, Any]) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._rate_limited_get(path, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise MusicBrainzError("MusicBrainz request failed") from error
                await self._sleep(min(4.0, 0.5 * (2**attempt)))
                continue
            if response.status_code == 404:
                raise MusicBrainzNotFound(path)
            if response.status_code in {429, 503} or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise MusicBrainzError(f"MusicBrainz unavailable ({response.status_code})")
                await self._sleep(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPStatusError, ValueError) as error:
                raise MusicBrainzError("invalid MusicBrainz response") from error
            if not isinstance(payload, dict):
                raise MusicBrainzError("unexpected MusicBrainz response shape")
            return payload
        raise MusicBrainzError("MusicBrainz request exhausted retries")

    async def _rate_limited_get(self, path: str, *, params: Mapping[str, Any]) -> httpx.Response:
        async with self._request_lock:
            delay = self._next_request_at - self._monotonic()
            if delay > 0:
                await self._sleep(delay)
            await self._host_rate_limiter.wait()
            try:
                return await self._client.get(path, params=params)
            finally:
                self._next_request_at = self._monotonic() + self._request_interval


class _HostRateLimiter:
    """Reserve request start times across every process sharing the state path."""

    def __init__(
        self,
        path: Path,
        *,
        interval: float,
        sleep: Callable[[float], Awaitable[None]],
        wall_clock: Callable[[], float],
    ) -> None:
        self._path = path
        self._interval = interval
        self._sleep = sleep
        self._wall_clock = wall_clock

    async def wait(self) -> None:
        lock_task = asyncio.create_task(asyncio.to_thread(_open_locked_rate_file, self._path))
        try:
            descriptor = await asyncio.shield(lock_task)
        except asyncio.CancelledError:
            # to_thread work cannot itself be cancelled. Wait for the short
            # reservation lock and release it so cancellation never leaks an
            # fd that would stall every process on the host.
            try:
                descriptor = await lock_task
            except OSError:
                raise
            _unlock_rate_file(descriptor)
            raise
        except OSError as error:
            raise MusicBrainzError("MusicBrainz host rate limiter is unavailable") from error
        try:
            last_started_at = _read_rate_timestamp(descriptor)
            now = self._wall_clock()
            delay = last_started_at + self._interval - now
            # A large future value means the wall clock changed or the tiny
            # state file was corrupted. Reset it instead of wedging requests.
            if 0 < delay <= max(10.0, self._interval * 10):
                await self._sleep(delay)
            _write_rate_timestamp(descriptor, self._wall_clock())
        except OSError as error:
            raise MusicBrainzError("MusicBrainz host rate limiter is unavailable") from error
        finally:
            _unlock_rate_file(descriptor)


def _open_locked_rate_file(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read_rate_timestamp(descriptor: int) -> float:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 64)
    try:
        value = float(raw.decode("ascii")) if raw else 0.0
    except (UnicodeDecodeError, ValueError):
        return 0.0
    return value if value >= 0 else 0.0


def _unlock_rate_file(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _write_rate_timestamp(descriptor: int, value: float) -> None:
    payload = f"{max(0.0, value):.6f}\n".encode("ascii")
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(descriptor, payload)
    os.ftruncate(descriptor, len(payload))


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(30.0, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(8.0, float(2**attempt))


def _entity(value: str, allowed: frozenset[str]) -> str:
    normalized = value.strip().casefold()
    if normalized not in allowed:
        raise ValueError(f"unsupported MusicBrainz entity: {value}")
    return normalized


def _mbid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid MusicBrainz identifier") from error


def _includes(values: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    unsupported = sorted(set(normalized) - INCLUDES)
    if unsupported:
        raise ValueError(f"unsupported MusicBrainz includes: {', '.join(unsupported)}")
    return normalized


def _bounded(value: int, lower: int, upper: int, label: str) -> int:
    if not lower <= value <= upper:
        raise ValueError(f"{label} must be between {lower} and {upper}")
    return value


def _lucene_phrase(value: str) -> str:
    return value.strip().replace("\\", "\\\\").replace('"', '\\"')
