from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.services.artist_credits import structured_artists
from app.sources.versions import normalize_match_text

_TITLE_SEPARATOR_RE = re.compile(r"\s+(?:-|\u2013|\u2014)\s+", re.UNICODE)
_PROVENANCE_NAME_RE = re.compile(
    r"(?:^|\b)(?:fan(?:page)?|archive|uploads?|channel|records?|recordings?|"
    r"label|media|network|vevo|topic|distribut(?:or|ion))(?:\b|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProviderRecordingMetadata:
    """Provider recording fields with account provenance kept separate."""

    artist: str | None
    title: str | None
    uploader: str | None
    artist_source: str | None
    title_source: str | None
    artists: tuple[str, ...] = ()


def resolve_provider_recording_metadata(
    metadata: Mapping[str, object],
    *,
    fallback_title: str | None = None,
    provider_artist_may_be_provenance: bool = False,
) -> ProviderRecordingMetadata:
    """Resolve bounded semantic fields without treating an uploader as an artist.

    ``artist`` and ``album_artist`` are provider track metadata and therefore take
    precedence. ``creator`` is lower-trust because yt-dlp extractors commonly use
    it for the publishing account. A well-formed ``Artist - Title`` display title
    wins over a creator that is the uploader or otherwise looks like provenance.

    ``provider_artist_may_be_provenance`` exists for persisted/legacy candidate
    rows where the original metadata key is unavailable. It lets ranking repair a
    previously copied uploader when the display title supplies a stronger pair.
    """

    uploader = _first_text(metadata, "uploader", "channel", "channel_name")
    artists = structured_artists(metadata.get("artists"))
    explicit_artist = _text(metadata.get("artist")) or (
        ", ".join(artists) if artists else _text(metadata.get("album_artist"))
    )
    creator = _text(metadata.get("creator"))
    track = _first_text(metadata, "track", "alt_title")
    display_title = _text(metadata.get("title")) or _text(fallback_title)

    pair_artist, pair_title = _strong_artist_title_pair(display_title or track)
    artist = explicit_artist
    artist_source = "artist" if _text(metadata.get("artist")) is not None or artists else None
    if artist is not None and artist_source is None:
        artist_source = "album_artist"

    demote_explicit = bool(
        artist
        and provider_artist_may_be_provenance
        and pair_artist
        and _identity_looks_like_provenance(artist, uploader)
        and normalize_match_text(artist) != normalize_match_text(pair_artist)
    )
    if demote_explicit:
        artist = None
        artist_source = None
        artists = ()

    if (
        artist is None
        and pair_artist is not None
        and (
            creator is None
            or _identity_looks_like_provenance(creator, uploader)
            or normalize_match_text(creator) == normalize_match_text(pair_artist)
        )
    ):
        artist = pair_artist
        artist_source = "parsed_title"
    elif (
        artist is None
        and creator is not None
        and not _identity_looks_like_provenance(creator, uploader)
    ):
        # A distinct, human-looking creator remains useful on providers that use
        # creator as their actual artist field. It never replaces explicit artist
        # metadata and never overrides a stronger parsed pair when it is account
        # provenance.
        artist = creator
        artist_source = "creator"
    elif artist is None and pair_artist is not None:
        artist = pair_artist
        artist_source = "parsed_title"

    if artist is not None and artist.casefold().endswith(" - topic"):
        artist = artist[:-8].strip() or None

    title_source: str | None
    if track is not None:
        track_artist, track_title = _strong_artist_title_pair(track)
        if track_artist is not None and (
            artist is None or normalize_match_text(track_artist) == normalize_match_text(artist)
        ):
            title = track_title
        else:
            title = track
        title_source = "track"
    elif pair_title is not None:
        title = pair_title
        title_source = "parsed_title"
    else:
        title = display_title
        title_source = "title" if display_title is not None else None

    return ProviderRecordingMetadata(
        artist=artist,
        title=title,
        uploader=uploader,
        artist_source=artist_source,
        title_source=title_source,
        artists=artists,
    )


def _strong_artist_title_pair(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    parts = _TITLE_SEPARATOR_RE.split(value.strip(), maxsplit=1)
    if len(parts) != 2:
        return None, None
    artist, title = (part.strip() for part in parts)
    if not artist or not title or len(artist) > 300 or len(title) > 500:
        return None, None
    if "://" in artist or "\n" in artist or "\r" in artist:
        return None, None
    return artist, title


def _identity_looks_like_provenance(value: str, uploader: str | None) -> bool:
    normalized = normalize_match_text(value)
    normalized_uploader = normalize_match_text(uploader or "")
    return bool(
        normalized and (normalized == normalized_uploader or _looks_like_provenance_name(value))
    )


def _looks_like_provenance_name(value: str) -> bool:
    return _PROVENANCE_NAME_RE.search(value) is not None


def _first_text(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        result = _text(value.get(key))
        if result is not None:
            return result
    return None


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
