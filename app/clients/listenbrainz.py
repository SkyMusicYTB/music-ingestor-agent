from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

import httpx

from app.config import Settings

LISTENBRAINZ_BASE_URL = "https://api.listenbrainz.org/"
STAT_ENTITIES = frozenset({"artists", "releases", "release-groups", "recordings"})
STAT_RANGES = frozenset(
    {
        "week",
        "month",
        "quarter",
        "half_yearly",
        "year",
        "all_time",
        "this_week",
        "this_month",
        "this_year",
    }
)


class ListenBrainzError(RuntimeError):
    pass


class ListenBrainzNotFound(ListenBrainzError):
    pass


class ListenBrainzClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_retries: int = 2,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": settings.musicbrainz_user_agent}
        if settings.listenbrainz_token is not None:
            token = settings.listenbrainz_token.get_secret_value().strip()
            if token:
                headers["Authorization"] = f"Token {token}"
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=LISTENBRAINZ_BASE_URL,
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        )
        self._client.headers.update(headers)
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_retries = max(0, max_retries)
        self._request_lock = asyncio.Lock()
        self._rate_remaining: int | None = None
        self._rate_reset_at = 0.0

    async def __aenter__(self) -> ListenBrainzClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def user_top(
        self,
        user_name: str,
        entity: str,
        *,
        range_name: str = "all_time",
        count: int = 25,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        return await self._stats(
            f"1/stats/user/{_path_component(user_name)}/{_stat_entity(entity)}",
            range_name=range_name,
            count=count,
            offset=offset,
        )

    async def sitewide_top(
        self,
        entity: str,
        *,
        range_name: str = "all_time",
        count: int = 25,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        return await self._stats(
            f"1/stats/sitewide/{_stat_entity(entity)}",
            range_name=range_name,
            count=count,
            offset=offset,
        )

    async def top_recordings_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET", f"1/popularity/top-recordings-for-artist/{_mbid(artist_mbid)}"
        )
        return _list_payload(payload)

    async def top_release_groups_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET", f"1/popularity/top-release-groups-for-artist/{_mbid(artist_mbid)}"
        )
        return _list_payload(payload)

    async def artist_radio(
        self,
        artist_mbid: str,
        *,
        mode: Literal["easy", "medium", "hard"] = "easy",
        max_similar_artists: int = 10,
        max_recordings_per_artist: int = 3,
        pop_begin: int = 0,
        pop_end: int = 100,
    ) -> dict[str, Any] | list[Any]:
        if mode not in {"easy", "medium", "hard"}:
            raise ValueError("invalid ListenBrainz radio mode")
        payload = await self._request_json(
            "GET",
            f"1/lb-radio/artist/{_mbid(artist_mbid)}",
            params={
                "mode": mode,
                "max_similar_artists": _bounded(max_similar_artists, 1, 50, "artists"),
                "max_recordings_per_artist": _bounded(
                    max_recordings_per_artist, 1, 25, "recordings"
                ),
                "pop_begin": _bounded(pop_begin, 0, 100, "pop_begin"),
                "pop_end": _bounded(pop_end, 0, 100, "pop_end"),
            },
        )
        if isinstance(payload, (dict, list)):
            return payload
        raise ListenBrainzError("unexpected artist radio response")

    async def recommendations(
        self, user_name: str, *, count: int = 25, offset: int = 0
    ) -> dict[str, Any] | None:
        payload = await self._request_json(
            "GET",
            f"1/cf/recommendation/user/{_path_component(user_name)}/recording",
            params={
                "count": _bounded(count, 1, 1000, "count"),
                "offset": _bounded(offset, 0, 100_000, "offset"),
            },
            allow_no_content=True,
        )
        return payload if isinstance(payload, dict) else None

    async def recommendation_playlists(self, user_name: str) -> dict[str, Any] | list[Any]:
        payload = await self._request_json(
            "GET", f"1/user/{_path_component(user_name)}/playlists/recommendations"
        )
        if isinstance(payload, (dict, list)):
            return payload
        raise ListenBrainzError("unexpected recommendation playlist response")

    async def similar_users(self, user_name: str) -> list[dict[str, Any]]:
        payload = await self._request_json(
            "GET", f"1/user/{_path_component(user_name)}/similar-users"
        )
        return _list_payload(payload)

    async def search_users(self, search_term: str) -> dict[str, Any] | list[Any]:
        term = search_term.strip()
        if not term:
            raise ValueError("ListenBrainz user search cannot be empty")
        payload = await self._request_json("GET", "1/search/users/", params={"search_term": term})
        if isinstance(payload, (dict, list)):
            return payload
        raise ListenBrainzError("unexpected user search response")

    async def search_playlists(self, query: str) -> dict[str, Any] | list[Any]:
        value = query.strip()
        if len(value) < 3:
            raise ValueError("ListenBrainz playlist search requires at least 3 characters")
        payload = await self._request_json("GET", "1/playlist/search", params={"query": value})
        if isinstance(payload, (dict, list)):
            return payload
        raise ListenBrainzError("unexpected playlist search response")

    async def recording_metadata(
        self,
        recording_mbids: Sequence[str],
        *,
        includes: Sequence[Literal["artist", "tag", "release"]] = ("artist", "release"),
    ) -> dict[str, Any] | list[Any]:
        mbids = [_mbid(value) for value in recording_mbids]
        if not mbids or len(mbids) > 1000:
            raise ValueError("recording metadata requires 1-1000 MBIDs")
        include_values = tuple(dict.fromkeys(includes))
        if set(include_values) - {"artist", "tag", "release"}:
            raise ValueError("unsupported ListenBrainz metadata include")
        payload = await self._request_json(
            "POST",
            "1/metadata/recording/",
            json_body={"recording_mbids": mbids, "inc": " ".join(include_values)},
        )
        if isinstance(payload, (dict, list)):
            return payload
        raise ListenBrainzError("unexpected metadata response")

    async def _stats(
        self,
        path: str,
        *,
        range_name: str,
        count: int,
        offset: int,
    ) -> dict[str, Any] | None:
        if range_name not in STAT_RANGES:
            raise ValueError("unsupported ListenBrainz statistics range")
        payload = await self._request_json(
            "GET",
            path,
            params={
                "range": range_name,
                "count": _bounded(count, 1, 1000, "count"),
                "offset": _bounded(offset, 0, 100_000, "offset"),
            },
            allow_no_content=True,
        )
        return payload if isinstance(payload, dict) else None

    async def _request_json(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        allow_no_content: bool = False,
    ) -> Any:
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._rate_limited_request(
                    method, path, params=params, json_body=json_body
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise ListenBrainzError("ListenBrainz request failed") from error
                await self._sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            if response.status_code == 204 and allow_no_content:
                return None
            if response.status_code == 404:
                raise ListenBrainzNotFound(path)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise ListenBrainzError(f"ListenBrainz unavailable ({response.status_code})")
                await self._sleep(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPStatusError, ValueError) as error:
                raise ListenBrainzError("invalid ListenBrainz response") from error
        raise ListenBrainzError("ListenBrainz request exhausted retries")

    async def _rate_limited_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
    ) -> httpx.Response:
        async with self._request_lock:
            if self._rate_remaining == 0:
                delay = self._rate_reset_at - self._monotonic()
                if delay > 0:
                    await self._sleep(delay)
            response = await self._client.request(method, path, params=params, json=json_body)
            self._update_rate_limit(response)
            return response

    def _update_rate_limit(self, response: httpx.Response) -> None:
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset_in = response.headers.get("X-RateLimit-Reset-In")
        try:
            self._rate_remaining = int(remaining) if remaining is not None else None
        except ValueError:
            self._rate_remaining = None
        try:
            seconds = max(0.0, float(reset_in)) if reset_in is not None else 0.0
        except ValueError:
            seconds = 0.0
        self._rate_reset_at = self._monotonic() + seconds


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    for header in ("Retry-After", "X-RateLimit-Reset-In"):
        value = response.headers.get(header)
        if value:
            try:
                return min(60.0, max(0.25, float(value)))
            except ValueError:
                continue
    return min(8.0, float(2**attempt))


def _path_component(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or "/" in normalized or ".." in normalized:
        raise ValueError("invalid ListenBrainz user name")
    return normalized


def _stat_entity(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    if normalized not in STAT_ENTITIES:
        raise ValueError("unsupported ListenBrainz statistics entity")
    return normalized


def _mbid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid MusicBrainz identifier") from error


def _bounded(value: int, lower: int, upper: int, label: str) -> int:
    if not lower <= value <= upper:
        raise ValueError(f"{label} must be between {lower} and {upper}")
    return value


def _list_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ListenBrainzError("unexpected ListenBrainz list response")
    return [value for value in payload if isinstance(value, dict)]
