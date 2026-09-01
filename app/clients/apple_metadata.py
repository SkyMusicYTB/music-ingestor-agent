from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

import httpx

from app.config import Settings

APPLE_SEARCH_BASE_URL = "https://itunes.apple.com/"
APPLE_ENTITIES = frozenset({"song", "album", "musicArtist", "musicTrack"})


class AppleMetadataError(RuntimeError):
    pass


class AppleMetadataDisabled(AppleMetadataError):
    pass


class AppleMetadataClient:
    """Conservative wrapper for Apple's legacy, unauthenticated Search API."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        request_interval: float = 3.0,
        max_retries: int = 1,
    ) -> None:
        self.enabled = settings.apple_metadata_enabled
        self.storefront = _country(settings.apple_storefront)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=APPLE_SEARCH_BASE_URL,
            headers={"Accept": "application/json", "User-Agent": settings.musicbrainz_user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
        )
        self._client.headers["Accept"] = "application/json"
        self._sleep = sleep
        self._monotonic = monotonic
        self._request_interval = max(3.0, request_interval)
        self._max_retries = max(0, max_retries)
        self._request_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def __aenter__(self) -> AppleMetadataClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        term: str,
        *,
        entity: Literal["song", "album", "musicArtist", "musicTrack"] = "song",
        limit: int = 25,
        country: str | None = None,
        explicit: bool = True,
    ) -> dict[str, Any]:
        self._require_enabled()
        query = term.strip()
        if not query:
            raise ValueError("Apple metadata search cannot be empty")
        if entity not in APPLE_ENTITIES:
            raise ValueError("unsupported Apple Search entity")
        return await self._get_json(
            "search",
            params={
                "term": query,
                "country": _country(country or self.storefront),
                "media": "music",
                "entity": entity,
                "limit": _bounded(limit, 1, 200),
                "explicit": "Yes" if explicit else "No",
                "version": 2,
            },
        )

    async def search_tracks(
        self,
        *,
        artist: str,
        title: str,
        limit: int = 10,
        country: str | None = None,
    ) -> dict[str, Any]:
        return await self.search(
            f"{artist.strip()} {title.strip()}",
            entity="song",
            limit=limit,
            country=country,
        )

    async def lookup(
        self,
        apple_id: int | str,
        *,
        entity: Literal["song", "album"] | None = None,
        limit: int = 25,
        country: str | None = None,
    ) -> dict[str, Any]:
        self._require_enabled()
        identifier = str(apple_id).strip()
        if not identifier.isdigit() or len(identifier) > 20:
            raise ValueError("invalid Apple catalog identifier")
        params: dict[str, Any] = {
            "id": identifier,
            "country": _country(country or self.storefront),
            "limit": _bounded(limit, 1, 200),
        }
        if entity is not None:
            params["entity"] = entity
        return await self._get_json("lookup", params=params)

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise AppleMetadataDisabled("Apple metadata fallback is disabled")

    async def _get_json(self, path: str, *, params: Mapping[str, Any]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._rate_limited_get(path, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise AppleMetadataError("Apple Search request failed") from error
                await self._sleep(1.0)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise AppleMetadataError(f"Apple Search unavailable ({response.status_code})")
                await self._sleep(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPStatusError, ValueError) as error:
                raise AppleMetadataError("invalid Apple Search response") from error
            if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
                raise AppleMetadataError("unexpected Apple Search response shape")
            return payload
        raise AppleMetadataError("Apple Search request exhausted retries")

    async def _rate_limited_get(self, path: str, *, params: Mapping[str, Any]) -> httpx.Response:
        async with self._request_lock:
            delay = self._next_request_at - self._monotonic()
            if delay > 0:
                await self._sleep(delay)
            try:
                return await self._client.get(path, params=params)
            finally:
                self._next_request_at = self._monotonic() + self._request_interval


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(30.0, max(3.0, float(value)))
        except ValueError:
            pass
    return max(3.0, float(2**attempt))


def _country(value: str) -> str:
    country = value.strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise ValueError("Apple storefront must be a two-letter country code")
    return country


def _bounded(value: int, lower: int, upper: int) -> int:
    if not lower <= value <= upper:
        raise ValueError(f"limit must be between {lower} and {upper}")
    return value
