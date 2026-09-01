from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import Settings

COVER_ART_BASE_URL = "https://coverartarchive.org/"


class CoverArtError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CoverArt:
    image_id: str
    image_url: str
    thumbnail_url: str
    mime_hint: str | None
    release_mbid: str | None
    release_group_mbid: str | None
    approved: bool


class CoverArtClient:
    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_retries: int = 2,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=COVER_ART_BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": settings.musicbrainz_user_agent,
            },
            follow_redirects=True,
            timeout=httpx.Timeout(20.0),
        )
        self._client.headers["Accept"] = "application/json"
        self._client.headers["User-Agent"] = settings.musicbrainz_user_agent
        self._sleep = sleep
        self._max_retries = max(0, max_retries)

    async def __aenter__(self) -> CoverArtClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def release(self, release_mbid: str) -> dict[str, Any] | None:
        return await self._listing(f"release/{_mbid(release_mbid)}")

    async def release_group(self, release_group_mbid: str) -> dict[str, Any] | None:
        return await self._listing(f"release-group/{_mbid(release_group_mbid)}")

    async def front(
        self,
        *,
        release_mbid: str | None = None,
        release_group_mbid: str | None = None,
        size: Literal[250, 500, 1200] = 500,
    ) -> CoverArt | None:
        if (release_mbid is None) == (release_group_mbid is None):
            raise ValueError("provide exactly one release or release-group MBID")
        listing = (
            await self.release(release_mbid)
            if release_mbid is not None
            else await self.release_group(str(release_group_mbid))
        )
        if listing is None:
            return None
        images = listing.get("images", [])
        if not isinstance(images, list):
            raise CoverArtError("unexpected Cover Art Archive response")
        candidates = [image for image in images if isinstance(image, Mapping)]
        if not candidates:
            return None
        selected = next((image for image in candidates if image.get("front") is True), None)
        if selected is None:
            selected = next(
                (
                    image
                    for image in candidates
                    if "Front" in image.get("types", []) and image.get("approved", True)
                ),
                None,
            )
        if selected is None:
            return None
        image_url = _safe_archive_url(str(selected.get("image") or ""))
        thumbnails = selected.get("thumbnails", {})
        if not isinstance(thumbnails, Mapping):
            thumbnails = {}
        thumbnail_url = _safe_archive_url(str(thumbnails.get(str(size)) or image_url))
        if not image_url or not thumbnail_url:
            raise CoverArtError("Cover Art Archive returned an unsafe image URL")
        source_release = _release_from_listing(listing)
        return CoverArt(
            image_id=str(selected.get("id") or ""),
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            mime_hint=_mime_hint(image_url),
            release_mbid=release_mbid or source_release,
            release_group_mbid=release_group_mbid,
            approved=bool(selected.get("approved", False)),
        )

    async def _listing(self, path: str) -> dict[str, Any] | None:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.get(path)
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise CoverArtError("Cover Art Archive request failed") from error
                await self._sleep(min(8.0, 0.5 * (2**attempt)))
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 503 or response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CoverArtError(f"Cover Art Archive unavailable ({response.status_code})")
                await self._sleep(_retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPStatusError, ValueError) as error:
                raise CoverArtError("invalid Cover Art Archive response") from error
            if not isinstance(payload, dict):
                raise CoverArtError("unexpected Cover Art Archive response shape")
            return payload
        raise CoverArtError("Cover Art Archive request exhausted retries")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(30.0, max(0.25, float(value)))
        except ValueError:
            pass
    return min(8.0, float(2**attempt))


def _mbid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid MusicBrainz identifier") from error


def _safe_archive_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except (TypeError, ValueError):
        return ""
    host = (url.host or "").casefold()
    known_host = (
        host == "coverartarchive.org"
        or host.endswith(".archive.org")
        or host == "archive.org"
        or host.endswith(".us.archive.org")
    )
    if not known_host or url.scheme not in {"http", "https"}:
        return ""
    # Older CAA documents can contain legacy http:// archive links. Upgrade
    # known archive hosts before exposing the URL to downstream download code.
    return str(url.copy_with(scheme="https"))


def _release_from_listing(listing: Mapping[str, Any]) -> str | None:
    value = str(listing.get("release") or "")
    if not value:
        return None
    candidate = value.rstrip("/").rsplit("/", 1)[-1]
    try:
        return _mbid(candidate)
    except ValueError:
        return None


def _mime_hint(value: str) -> str | None:
    path = httpx.URL(value).path.casefold()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return None
