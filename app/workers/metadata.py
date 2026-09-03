from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import uuid
from collections.abc import Awaitable, Callable, Mapping
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
from app.services.artist_credits import (
    artist_credit_similarity,
    artist_credit_variant,
    structured_artists,
)
from app.services.metadata_matching import (
    MatchResult,
    MetadataCandidate,
    MetadataMatcher,
    candidates_from_musicbrainz,
    normalize_text,
)
from app.services.metadata_matching import (
    version_signature as classify_version,
)

_NONSTANDARD_EDITION = re.compile(
    r"\b(compilation|greatest hits|best of|deluxe|expanded|anniversary|remaster|reissue)\b",
    re.IGNORECASE,
)
MAX_METADATA_SEARCH_REQUESTS = 12
MAX_RECORDING_SEARCHES = 7
MAX_RECORDING_CANDIDATES = 100


class WorkerMetadataError(RuntimeError):
    """A bounded, classified failure of the canonical-metadata provider."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "temporary_failure",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CanonicalMetadataResolution:
    decision: Literal["auto", "review", "reject"]
    candidate: MetadataCandidate | None
    options: tuple[dict[str, Any], ...]
    reason: str
    reason_code: str = "matched"


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
        artists: tuple[str, ...] = (),
        year: int | None = None,
        isrc: str | None = None,
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
                artists=artists,
                year=year,
                isrc=isrc,
            ),
            self._loop,
        )
        try:
            return future.result(timeout=self.timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise WorkerMetadataError("MusicBrainz resolution timed out") from exc
        except MusicBrainzError as exc:
            raise WorkerMetadataError(
                "MusicBrainz resolution failed",
                reason_code=exc.reason_code,
                retryable=exc.retryable,
            ) from exc

    async def _resolve_async(
        self,
        *,
        artist: str,
        title: str,
        album: str | None,
        duration_seconds: float | None,
        version_signature: str | None,
        album_is_explicit: bool = False,
        artists: tuple[str, ...] = (),
        year: int | None = None,
        isrc: str | None = None,
    ) -> CanonicalMetadataResolution:
        artists = structured_artists(artists)
        budget = _RequestBudget()
        candidates: dict[str, MetadataCandidate] = {}
        saw_candidates = False

        def rank() -> list[MatchResult]:
            return self._matcher.rank(
                artist=artist,
                artists=artists,
                title=title,
                album=album,
                duration_seconds=duration_seconds,
                requested_version=version_signature,
                requested_year=year,
                requested_isrc=isrc,
                album_is_explicit=album_is_explicit,
                version_is_explicit=version_signature is not None,
                candidates=candidates.values(),
                limit=8,
            )

        def collect(payload: dict[str, Any], *, broad_search: bool) -> None:
            nonlocal saw_candidates
            values = payload.get("recordings")
            if not isinstance(values, list):
                raise MusicBrainzError(
                    "unexpected MusicBrainz recordings shape",
                    reason_code="malformed_response",
                    retryable=False,
                )
            parsed = candidates_from_musicbrainz({"recordings": values[:MAX_RECORDING_CANDIDATES]})
            saw_candidates = saw_candidates or bool(parsed)
            if values and not parsed:
                raise MusicBrainzError(
                    "MusicBrainz recording candidates were malformed",
                    reason_code="malformed_response",
                    retryable=False,
                )
            for candidate in parsed:
                candidate = _with_sensible_release(
                    candidate, requested_album=album if album_is_explicit else None
                )
                if broad_search and not _safe_broad_candidate(
                    candidate,
                    artist=artist,
                    artists=artists,
                    title=title,
                    duration_seconds=duration_seconds,
                    version_signature=version_signature,
                ):
                    continue
                if candidate.recording_mbid and (
                    candidate.recording_mbid in candidates
                    or len(candidates) < MAX_RECORDING_CANDIDATES
                ):
                    existing = candidates.get(candidate.recording_mbid)
                    candidates[candidate.recording_mbid] = _merge_candidate(
                        existing,
                        candidate,
                        requested_album=album if album_is_explicit else None,
                    )

        ranked: list[MatchResult] = []
        for search_artist, artist_terms in _recording_queries(artist, artists):
            payload = await budget.call(
                self._client.search_recordings,
                artist=search_artist,
                title=title,
                artist_terms=artist_terms,
                limit=25 if search_artist is None and not artist_terms else 10,
            )
            if payload is None:
                break
            collect(payload, broad_search=search_artist is None and not artist_terms)
            ranked = rank()
            if ranked and ranked[0].decision == "auto":
                break

        if not ranked or ranked[0].decision != "auto":
            # Some recordings are only discoverable from a release's tracklist.
            # Search a bounded release/group set and inspect only returned IDs.
            release_title = album if album_is_explicit and album else title
            release_artist = artist_credit_variant(artist)
            release_ids: list[str] = []
            releases = await budget.call(
                self._client.search_releases,
                artist=release_artist,
                title=release_title,
                limit=2,
            )
            release_ids.extend(_result_ids(releases, "releases", limit=2))
            if not release_ids:
                groups = await budget.call(
                    self._client.search_release_groups,
                    artist=release_artist,
                    title=release_title,
                    limit=1,
                )
                for group_id in _result_ids(groups, "release-groups", limit=1):
                    releases = await budget.call(
                        self._client.browse,
                        "release",
                        "release-group",
                        group_id,
                        limit=2,
                        includes=("artist-credits",),
                    )
                    release_ids.extend(_result_ids(releases, "releases", limit=2))
            for release_id in dict.fromkeys(release_ids):
                release = await budget.call(
                    self._client.lookup,
                    "release",
                    release_id,
                    includes=("recordings", "artist-credits", "release-groups"),
                )
                if release is not None:
                    records = _release_recordings(release)
                    if records:
                        collect({"recordings": records}, broad_search=True)
                ranked = rank()
                if ranked and ranked[0].decision == "auto":
                    break

        ranked = rank()
        if not ranked:
            return CanonicalMetadataResolution(
                decision="reject",
                candidate=None,
                options=(),
                reason=(
                    "No MusicBrainz recording candidate satisfied identity checks"
                    if saw_candidates
                    else "MusicBrainz returned no recording candidates"
                ),
                reason_code="low_confidence" if saw_candidates else "no_candidates",
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
                "artists": list(item.candidate.artists),
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
            reason_code="matched" if top.decision == "auto" else "low_confidence",
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


class _RequestBudget:
    def __init__(self) -> None:
        self.used = 0

    async def call(
        self,
        operation: Callable[..., Awaitable[dict[str, Any] | None]],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        if self.used >= MAX_METADATA_SEARCH_REQUESTS:
            return None
        self.used += 1
        return await operation(*args, **kwargs)


def _recording_queries(
    artist: str, artists: tuple[str, ...]
) -> tuple[tuple[str | None, tuple[str, ...]], ...]:
    queries: list[tuple[str | None, tuple[str, ...]]] = [(artist, ())]
    variant = artist_credit_variant(artist)
    if variant != artist:
        queries.append((variant, ()))
    # Only provider-supplied structured artists are individual search terms.
    # Do not turn punctuation-bearing band names into invented collaborators.
    for collaborator in artists[:3]:
        entry = (collaborator, ())
        if entry not in queries:
            queries.append(entry)
    if len(artists) > 1:
        queries.append((None, artists[:4]))
    queries = queries[: MAX_RECORDING_SEARCHES - 1]
    queries.append((None, ()))
    return tuple(queries)


def _safe_broad_candidate(
    candidate: MetadataCandidate,
    *,
    artist: str,
    artists: tuple[str, ...],
    title: str,
    duration_seconds: float | None,
    version_signature: str | None,
) -> bool:
    """A title-only/release search is discovery, never identity authority."""
    return bool(
        normalize_text(title) == normalize_text(candidate.title)
        and artist_credit_similarity(
            artist, candidate.artist, left_artists=artists, right_artists=candidate.artists
        )
        >= 0.95
        and duration_seconds is not None
        and candidate.duration_seconds is not None
        and abs(duration_seconds - candidate.duration_seconds) <= max(10, duration_seconds * 0.05)
        and classify_version(version_signature, title) == candidate.version
    )


def _merge_candidate(
    existing: MetadataCandidate | None,
    candidate: MetadataCandidate,
    *,
    requested_album: str | None,
) -> MetadataCandidate:
    if existing is None:
        return candidate
    # The same recording returned by multiple searches must not compete with
    # itself and erase its eight-point lead. Preserve already enriched fields.
    releases: dict[str, dict[str, Any]] = {}
    for raw in (existing.raw, candidate.raw):
        values = raw.get("releases") if raw else None
        if isinstance(values, list):
            for value in values[:100]:
                if isinstance(value, dict) and isinstance(value.get("id"), str):
                    releases.setdefault(value["id"], value)
    raw = {**(candidate.raw or {}), **(existing.raw or {}), "releases": list(releases.values())}
    merged = replace(
        existing,
        album=existing.album or candidate.album,
        year=existing.year or candidate.year,
        duration_seconds=existing.duration_seconds or candidate.duration_seconds,
        release_mbid=existing.release_mbid or candidate.release_mbid,
        release_group_mbid=existing.release_group_mbid or candidate.release_group_mbid,
        artists=existing.artists or candidate.artists,
        raw=raw,
    )
    return _with_sensible_release(merged, requested_album=requested_album)


def _result_ids(payload: dict[str, Any] | None, key: str, *, limit: int) -> list[str]:
    if payload is None:
        return []
    values = payload.get(key)
    if not isinstance(values, list):
        raise MusicBrainzError(
            "unexpected MusicBrainz search shape",
            reason_code="malformed_response",
            retryable=False,
        )
    result: list[str] = []
    for value in values[:limit]:
        if isinstance(value, Mapping) and isinstance(value.get("id"), str):
            try:
                result.append(str(uuid.UUID(value["id"])))
            except ValueError:
                continue
    return result


def _provider_mbid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _release_recordings(release: dict[str, Any]) -> list[dict[str, Any]]:
    media = release.get("media")
    if not isinstance(media, list):
        return []
    result: list[dict[str, Any]] = []
    # Bound traversal independently of the provider's declared counts.
    for medium in media[:20]:
        if not isinstance(medium, dict) or not isinstance(medium.get("tracks"), list):
            continue
        for track in medium["tracks"][:100]:
            if len(result) >= 100:
                return result
            if not isinstance(track, dict) or not isinstance(track.get("recording"), dict):
                continue
            recording = dict(track["recording"])
            recording.setdefault("title", track.get("title"))
            recording.setdefault("length", track.get("length"))
            recording.setdefault(
                "artist-credit", track.get("artist-credit") or release.get("artist-credit")
            )
            recording.setdefault("first-release-date", release.get("date"))
            recording["releases"] = [
                {key: value for key, value in release.items() if key != "media"}
            ]
            result.append(recording)
    return result


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
        release_mbid=_provider_mbid(selected.get("id")) or candidate.release_mbid,
        release_group_mbid=(
            _provider_mbid(release_group.get("id")) or candidate.release_group_mbid
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
