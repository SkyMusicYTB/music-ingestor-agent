"""Metadata enrichment policy; this module cannot authorize a media source."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.services.artist_credits import artist_credit_similarity, structured_artists
from app.services.duplicates import normalize_text, strip_provider_suffixes
from app.sources import DEFAULT_VERSION_CLASSIFIER, resolve_provider_recording_metadata

FALLBACK_WARNING = (
    "No confident MusicBrainz match was found. Validated source metadata was used instead."
)
FALLBACK_AUTHORITIES = frozenset(
    {"validated_provider", "direct_user_source", "user_confirmed_provider_metadata"}
)


@dataclass(frozen=True, slots=True)
class SourceAuthority:
    validated: bool = False
    direct_approved: bool = False
    local_score: float = 0.0
    contradictions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderFallback:
    payload: dict[str, object]
    automatic: bool


def provider_fallback(
    values: Mapping[str, object],
    source: Mapping[str, object],
    *,
    authority: SourceAuthority,
    duration: float,
    minimum_score: float,
) -> ProviderFallback | None:
    """Build only from probed recording fields, never uploader or model suggestions.

    The caller first enforces all URL/probe/version/source identity hard guards.
    A review option can be returned for coherent evidence below the automatic
    threshold, but an unrelated artist/title or version is never offered.
    """
    recording = resolve_provider_recording_metadata(source)
    artist = _text(recording.artist, 300)
    title = _text(strip_provider_suffixes(recording.title or ""), 300)
    if not authority.validated or artist is None or title is None:
        return None
    if recording.artist_source == "creator" and artist == recording.uploader:
        return None
    requested_artist = _text(values.get("artist"), 300) or ""
    requested_title = strip_provider_suffixes(_text(values.get("title"), 300) or "")
    artist_score = artist_credit_similarity(
        requested_artist,
        artist,
        left_artists=structured_artists(values.get("artists")),
        right_artists=recording.artists,
    )
    title_score = SequenceMatcher(
        None, normalize_text(requested_title), normalize_text(title), autojunk=False
    ).ratio()
    expected_version = DEFAULT_VERSION_CLASSIFIER.classify(
        requested_title, _text(values.get("requested_version"), 100) or "studio"
    )
    actual_version = DEFAULT_VERSION_CLASSIFIER.classify(
        _text(source.get("title"), 500), title, _text(source.get("version"), 100)
    )
    compatible = DEFAULT_VERSION_CLASSIFIER.compatible(expected_version, actual_version)
    expected_duration = _number(values.get("duration_seconds"))
    duration_ok = expected_duration is None or abs(expected_duration - duration) <= max(
        10.0, expected_duration * 0.05
    )
    if (
        authority.contradictions
        or not compatible
        or not duration_ok
        or artist_score < 0.85
        or title_score < 0.90
    ):
        return None
    # Unknown duration cannot authorize a fuzzy unattended fallback. The caller
    # supplies a real probe duration, or the verified audio duration after download.
    strong_identity = artist_score >= 0.95 and title_score >= 0.97
    automatic = strong_identity and (
        authority.direct_approved or authority.local_score >= minimum_score
    )
    album = _text(source.get("album"), 300)
    album_artist = _text(source.get("album_artist"), 300) or artist
    year = source.get("release_year")
    date = _text(source.get("release_date"), 10)
    if not isinstance(year, int) or isinstance(year, bool) or not 1000 <= year <= 2999:
        year = int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None
    if year is not None and not 1000 <= year <= 2999:
        year = None
    return ProviderFallback(
        payload={
            "kind": "canonical_metadata",
            "rank": 1,
            "label": "Use validated source metadata (or correct it below)",
            "artist": artist,
            "artists": list(recording.artists or (artist,)),
            "title": title,
            "album": album,
            "album_artist": album_artist,
            "year": year,
            "duration_seconds": duration,
            "version": actual_version.signature,
            "metadata_authority": (
                "direct_user_source" if authority.direct_approved else "validated_provider"
            ),
            "canonical_identity_verified": False,
            "recording_mbid": None,
            "release_mbid": None,
            "release_group_mbid": None,
            "source_provider": _text(values.get("source_provider"), 40),
            "source_extractor": _text(values.get("source_extractor"), 80),
            "source_id": _text(values.get("source_id"), 200),
            "source_url": _text(values.get("source_url"), 2048),
            "source_uploader": _text(recording.uploader, 300),
            "score": authority.local_score,
            "local_score": authority.local_score,
            "reason_codes": ["validated_provider_metadata"],
        },
        automatic=automatic,
    )


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    clean = " ".join(value.split())[:limit]
    return clean or None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    return None
