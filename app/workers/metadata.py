from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx

from app.clients.musicbrainz import (
    MUSICBRAINZ_BASE_URL,
    MusicBrainzClient,
    MusicBrainzError,
)
from app.config import Settings
from app.services.metadata_matching import (
    MetadataCandidate,
    MetadataMatcher,
    candidates_from_musicbrainz,
    normalize_text,
)

_NONSTANDARD_EDITION = re.compile(
    r"\b(compilation|greatest hits|best of|deluxe|expanded|anniversary|remaster|reissue)\b",
    re.IGNORECASE,
)


class WorkerMetadataError(RuntimeError):
    """A retryable failure of the public canonical-metadata provider."""


@dataclass(frozen=True, slots=True)
class CanonicalMetadataResolution:
    decision: Literal["auto", "review", "reject"]
    candidate: MetadataCandidate | None
    options: tuple[dict[str, Any], ...]
    reason: str


class MusicBrainzWorkerResolver:
    """Synchronous worker bridge to one process-shared 1 request/second client.

    All download slots submit their searches to this resolver's single event loop.
    The underlying MusicBrainzClient lock therefore serializes both delay and send.
    """

    def __init__(self, settings: Settings, *, timeout_seconds: float = 55.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._http = httpx.AsyncClient(
            base_url=MUSICBRAINZ_BASE_URL,
            headers={"Accept": "application/json"},
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            trust_env=False,
        )
        self._client = MusicBrainzClient(settings, http_client=self._http)
        self._matcher = MetadataMatcher()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="musicbrainz-worker-client",
            daemon=True,
        )
        self._closed = False
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def resolve(
        self,
        *,
        artist: str,
        title: str,
        album: str | None,
        duration_seconds: float | None,
        version_signature: str | None,
        album_is_explicit: bool = False,
    ) -> CanonicalMetadataResolution:
        if self._closed:
            raise WorkerMetadataError("MusicBrainz resolver is closed")
        future = asyncio.run_coroutine_threadsafe(
            self._resolve_async(
                artist=artist,
                title=title,
                album=album,
                duration_seconds=duration_seconds,
                version_signature=version_signature,
                album_is_explicit=album_is_explicit,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise WorkerMetadataError("MusicBrainz resolution timed out") from exc
        except MusicBrainzError as exc:
            raise WorkerMetadataError("MusicBrainz resolution failed") from exc

    async def _resolve_async(
        self,
        *,
        artist: str,
        title: str,
        album: str | None,
        duration_seconds: float | None,
        version_signature: str | None,
        album_is_explicit: bool = False,
    ) -> CanonicalMetadataResolution:
        payload = await self._client.search_recordings(artist=artist, title=title, limit=10)
        candidates = [
            _with_sensible_release(
                candidate,
                requested_album=album if album_is_explicit else None,
            )
            for candidate in candidates_from_musicbrainz(payload)
        ]
        ranked = self._matcher.rank(
            artist=artist,
            title=title,
            album=album,
            duration_seconds=duration_seconds,
            requested_version=version_signature,
            album_is_explicit=album_is_explicit,
            version_is_explicit=version_signature is not None,
            candidates=candidates,
            limit=8,
        )
        if not ranked:
            return CanonicalMetadataResolution(
                decision="reject",
                candidate=None,
                options=(),
                reason="MusicBrainz returned no recording candidates",
            )
        options = tuple(
            {
                "kind": "canonical_metadata",
                "rank": index,
                "recording_candidate_id": _candidate_id(
                    "rec",
                    item.candidate.recording_mbid,
                    item.candidate.artist,
                    item.candidate.title,
                ),
                "release_candidate_id": (
                    _candidate_id(
                        "rel",
                        item.candidate.release_mbid,
                        item.candidate.album,
                        str(item.candidate.year or ""),
                    )
                    if item.candidate.release_mbid
                    else None
                ),
                "artist": item.candidate.artist,
                "title": item.candidate.title,
                "album": item.candidate.album,
                "year": item.candidate.year,
                "duration_seconds": item.candidate.duration_seconds,
                "recording_mbid": item.candidate.recording_mbid,
                "release_mbid": item.candidate.release_mbid,
                "release_group_mbid": item.candidate.release_group_mbid,
                "score": item.score / 100.0,
                "local_score": item.score / 100.0,
                "version": item.candidate.version,
                **_selected_release_summary(item.candidate),
                "reason_codes": list(item.reasons),
                "reasons": list(item.reasons),
                "contradiction_codes": list(item.contradiction_codes),
            }
            for index, item in enumerate(ranked, start=1)
            if item.score >= 70
        )
        top = ranked[0]
        return CanonicalMetadataResolution(
            decision=top.decision,
            candidate=top.candidate,
            options=options,
            reason=(
                f"MusicBrainz canonical match scored {top.score:.1f}/100"
                + (f" with a {top.lead:.1f}-point lead" if top.lead is not None else "")
            ),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        future = asyncio.run_coroutine_threadsafe(self._http.aclose(), self._loop)
        try:
            future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            self._loop.close()


def _candidate_id(prefix: str, *values: str | None) -> str:
    material = "\x1f".join(value or "" for value in values)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:20]}"


def _with_sensible_release(
    candidate: MetadataCandidate, *, requested_album: str | None
) -> MetadataCandidate:
    raw = candidate.raw
    if not isinstance(raw, dict):
        return candidate
    releases = raw.get("releases")
    if not isinstance(releases, list):
        return candidate
    valid = [release for release in releases if isinstance(release, dict)]
    if not valid:
        return candidate
    requested = normalize_text(requested_album)

    def release_key(release: dict[str, Any]) -> tuple[object, ...]:
        title = str(release.get("title") or "")
        release_group = release.get("release-group")
        group = release_group if isinstance(release_group, dict) else {}
        secondary = group.get("secondary-types")
        secondary_types = secondary if isinstance(secondary, list) else []
        status = normalize_text(str(release.get("status") or ""))
        primary = normalize_text(str(group.get("primary-type") or ""))
        date = str(release.get("date") or raw.get("first-release-date") or "9999")
        year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 9999
        normalized_title = normalize_text(title)
        explicit_album = int(bool(requested) and normalized_title == requested)
        official = int(status == "official")
        standard = int(
            not secondary_types
            and not _NONSTANDARD_EDITION.search(
                " ".join([title, *(str(item) for item in secondary_types)])
            )
        )
        canonical_type = 2 if primary == "album" else 1 if primary == "single" else 0
        # Higher semantic precedence first, then the earliest sensible edition.
        return (-explicit_album, -official, -standard, -canonical_type, year, normalized_title)

    selected = min(valid, key=release_key)
    group_value = selected.get("release-group")
    release_group = group_value if isinstance(group_value, dict) else {}
    date = str(selected.get("date") or raw.get("first-release-date") or "")
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else candidate.year
    return replace(
        candidate,
        album=str(selected.get("title")) if selected.get("title") else candidate.album,
        year=year,
        release_mbid=(str(selected.get("id")) if selected.get("id") else candidate.release_mbid),
        release_group_mbid=(
            str(release_group.get("id"))
            if release_group.get("id")
            else candidate.release_group_mbid
        ),
    )


def _selected_release_summary(candidate: MetadataCandidate) -> dict[str, str | None]:
    """Return provider facts for the already selected release, without inventing them."""

    raw = candidate.raw
    if not isinstance(raw, dict):
        return {"release_status": None, "primary_type": None}
    releases = raw.get("releases")
    if not isinstance(releases, list):
        return {"release_status": None, "primary_type": None}
    for item in releases:
        if not isinstance(item, dict) or str(item.get("id") or "") != candidate.release_mbid:
            continue
        group_value = item.get("release-group")
        group = group_value if isinstance(group_value, dict) else {}
        status = str(item.get("status") or "").strip() or None
        primary_type = str(group.get("primary-type") or "").strip() or None
        return {"release_status": status, "primary_type": primary_type}
    return {"release_status": None, "primary_type": None}


__all__ = [
    "CanonicalMetadataResolution",
    "MusicBrainzWorkerResolver",
    "WorkerMetadataError",
]
