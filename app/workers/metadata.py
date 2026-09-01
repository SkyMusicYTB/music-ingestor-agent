from __future__ import annotations

import asyncio
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
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
        version_signature: str,
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
        version_signature: str,
    ) -> CanonicalMetadataResolution:
        payload = await self._client.search_recordings(artist=artist, title=title, limit=10)
        candidates = candidates_from_musicbrainz(payload)
        ranked = self._matcher.rank(
            artist=artist,
            title=title,
            album=album,
            duration_seconds=duration_seconds,
            requested_version=version_signature,
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
                "kind": "metadata",
                "rank": index,
                "artist": item.candidate.artist,
                "title": item.candidate.title,
                "album": item.candidate.album,
                "year": item.candidate.year,
                "duration_seconds": item.candidate.duration_seconds,
                "recording_mbid": item.candidate.recording_mbid,
                "release_mbid": item.candidate.release_mbid,
                "release_group_mbid": item.candidate.release_group_mbid,
                "score": item.score / 100.0,
                "reasons": list(item.reasons),
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


__all__ = [
    "CanonicalMetadataResolution",
    "MusicBrainzWorkerResolver",
    "WorkerMetadataError",
]
