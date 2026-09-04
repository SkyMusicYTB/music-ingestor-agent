from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from app.config import Settings
from app.db.models import Request, RequestTrack
from app.services.artist_credits import artist_credit_similarity, structured_artists
from app.services.duplicates import normalize_text, versions_compatible
from app.services.request_constraints import parse_explicit_request_constraints


@dataclass(frozen=True)
class ConfirmationDecision:
    auto_queue: bool
    reason: str


_ACTION_PREFIX = re.compile(
    r"^(?:please\s+)?(?:add|download|queue|save|import|get)\s+(?:me\s+)?",
    re.IGNORECASE,
)
_CALLED_TRACK = re.compile(
    r"^(?:the\s+)?(?:song|track|recording)\s+(?:called|named|titled)\s+(.+)$",
    re.IGNORECASE,
)
_BY_ARTIST = re.compile(r"^(.+?)\s+by\s+(.+)$", re.IGNORECASE)
_ARTIST_TITLE = re.compile(r"^(.+?)\s+[-\u2013\u2014]\s+(.+)$")
_FUZZY_LANGUAGE = re.compile(
    r"\b(?:recommend|suggest|discover|similar|like|vibes?|moods?|genres?|playlists?|"
    r"mix(?:es)?|radio|random|surprise|some|any|something|anything|songs|tracks|"
    r"recordings|music|favorites?|favourites?|popular|best|top|dreamy|relaxing|chill|"
    r"upbeat|energetic|workout|party|hits?|singles?|newest|latest|recent|current)\b",
    re.IGNORECASE,
)
_RELATIONAL_FUZZY_LANGUAGE = re.compile(
    r"\b(?:recommend|suggest|discover|similar|like|vibes?|moods?|genres?|playlists?|"
    r"mix(?:es)?|radio|random|surprise)\b",
    re.IGNORECASE,
)
_GENERIC_TRACK_DESCRIPTION = re.compile(
    r"(?:\b(?:song|track|recording|music|vibe|playlist|mix)\b\s*$|"
    r"^(?:a|an|the|some|any|one|\d+)\s+(?:\S+\s+){0,8}"
    r"(?:song|track|recording|piece|hit|single)\b)",
    re.IGNORECASE,
)
_SOURCE_PROVIDER = r"(?:bandcamp|soundcloud|youtube)"
_SOURCE_QUALIFIER_SUFFIXES = (
    re.compile(
        rf"\s*[,;]?\s+(?:and|or)\s+"
        rf"(?:(?:from|via|using|through|on)\s+(?:the\s+)?)?"
        rf"{_SOURCE_PROVIDER}(?:\s+(?:only|exclusively))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\s*[,;]?\s+(?:not\s+(?:(?:from|on|via|using|through)\s+)?|"
        rf"except(?:\s+for)?\s+|without\s+)(?:the\s+)?{_SOURCE_PROVIDER}\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\s*[,;]?\s+(?:from|via|using|through|on)\s+(?:the\s+)?"
        rf"{_SOURCE_PROVIDER}(?:\s+(?:only|exclusively))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\s+{_SOURCE_PROVIDER}\s+(?:only|exclusively)\s*$",
        re.IGNORECASE,
    ),
)
_ALBUM_QUALIFIER_BEFORE_ARTIST = re.compile(
    r"\s+(?:from|on)\s+(?:the\s+)?(?:album|release)\s+"
    r"(?:\"[^\"]+\"|'[^']+'|\S(?:.*?\S)?)\s*(?=\s+by\s+)",
    re.IGNORECASE,
)
_ALBUM_QUALIFIER_SUFFIX = re.compile(
    r"\s+(?:from|on)\s+(?:the\s+)?(?:album|release)\s+.+$",
    re.IGNORECASE,
)
_VERSION_QUALIFIER = re.compile(
    r"\s*[\[(]\s*(?:live|acoustic|remix(?:ed)?|demo|instrumental|karaoke|cover|"
    r"radio\s+edit|sped[ -]?up|slowed(?:\s*[+&]\s*reverb)?|remaster(?:ed)?(?:\s+\d{4})?|"
    r"extended(?:\s+mix)?)(?:\s+(?:version|recording))?\s*[\])]",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _RequestedTrackIdentity:
    title: str
    artist: str | None


def confirmation_decision(
    request: Request, tracks: list[RequestTrack], settings: Settings
) -> ConfirmationDecision:
    if request.action != "add":
        return ConfirmationDecision(False, "find requests always require preview")
    if not settings.auto_download_exact_single:
        return ConfirmationDecision(False, "automatic exact-track acquisition is disabled")
    selected = [track for track in tracks if track.selected]
    if len(selected) != 1 or len(tracks) != 1:
        return ConfirmationDecision(False, "multiple or competing candidates require approval")
    track = selected[0]
    if _has_collection_origin(request, track):
        return ConfirmationDecision(False, "collection entries require explicit track selection")
    if not _request_proves_exact_track_intent(request, track):
        return ConfirmationDecision(
            False,
            "the original request does not match one authoritative exact-track identity",
        )
    if track.duplicate_status != "none":
        return ConfirmationDecision(False, "duplicate decisions require review")
    exact_direct_source = bool(
        request.input_kind in {"youtube_url", "media_url"}
        and track.source_extractor
        and track.source_id
    )
    if not exact_direct_source:
        if (track.metadata_confidence or 0.0) < 0.88:
            return ConfirmationDecision(
                False, "metadata confidence is below the exact-match threshold"
            )
        if not _has_verified_automatic_association(track):
            return ConfirmationDecision(
                False, "canonical identity was not verified by the deterministic matcher"
            )
    if not (track.recording_mbid or (track.source_extractor and track.source_id)):
        return ConfirmationDecision(False, "candidate lacks an exact canonical or source identity")
    if request.requested_count not in (None, 1):
        return ConfirmationDecision(False, "bulk requests require approval")
    return ConfirmationDecision(True, "single exact high-confidence Add request")


def _request_proves_exact_track_intent(request: Request, track: RequestTrack) -> bool:
    """Prove exact intent from authoritative request input, never model output.

    A reviewed direct media URL is inherently a single concrete source. Natural
    language is deliberately conservative: discovery, mood, similarity, genre,
    and collection language remains a preview even when orchestration happens to
    return one high-confidence recording.
    """

    if request.input_kind in {"youtube_url", "media_url"}:
        return True
    if request.input_kind != "natural_language":
        return False
    raw_text = getattr(request, "raw_text", None)
    if not isinstance(raw_text, str):
        return False
    value = " ".join(unicodedata.normalize("NFKC", raw_text).split()).strip()
    if not value or len(value) > 600:
        return False
    value = _ACTION_PREFIX.sub("", value, count=1).strip(" \t:;,.!")
    if not value:
        return False

    constraints = parse_explicit_request_constraints(value, input_kind="natural_language")
    value = _strip_source_qualifiers(value)
    value = _ALBUM_QUALIFIER_BEFORE_ARTIST.sub("", value).strip()
    value = _ALBUM_QUALIFIER_SUFFIX.sub("", value).strip()
    value = _VERSION_QUALIFIER.sub("", value).strip()

    called = _CALLED_TRACK.fullmatch(value)
    if called:
        value = called.group(1).strip()

    identity = _parse_requested_track_identity(value)
    if identity is None:
        return False
    track_title = getattr(track, "title", None)
    track_artist = getattr(track, "artist", None)
    if not isinstance(track_title, str) or normalize_text(identity.title) != normalize_text(
        track_title
    ):
        return False
    if identity.artist is not None and (
        not isinstance(track_artist, str)
        or artist_credit_similarity(
            identity.artist,
            track_artist,
            right_artists=_track_structured_artists(track),
        )
        < 0.95
    ):
        return False
    track_album = getattr(track, "album", None)
    if constraints.album is not None and (
        not isinstance(track_album, str)
        or normalize_text(constraints.album) != normalize_text(track_album)
    ):
        return False
    if constraints.version is not None and not versions_compatible(
        constraints.version,
        getattr(track, "version_signature", None),
    ):
        return False
    return True


def _track_structured_artists(track: RequestTrack) -> tuple[str, ...]:
    """Read only locally persisted, bounded canonical artist-credit parts."""

    encoded = getattr(track, "metadata_provenance_json", None)
    if not isinstance(encoded, str):
        return ()
    try:
        provenance = json.loads(encoded)
    except (TypeError, ValueError):
        return ()
    if not isinstance(provenance, dict):
        return ()
    return structured_artists(provenance.get("artists"))


def _parse_requested_track_identity(value: str) -> _RequestedTrackIdentity | None:
    by_artist = _BY_ARTIST.fullmatch(value)
    if by_artist:
        title, artist = (_clean_identity_part(part) for part in by_artist.groups())
        if not title or not artist:
            return None
        # An explicit artist disambiguates otherwise generic real titles such as
        # "Something" or "Song 2". Relational or descriptive requests remain fuzzy.
        if (
            _RELATIONAL_FUZZY_LANGUAGE.search(title)
            or _GENERIC_TRACK_DESCRIPTION.search(title)
            or _RELATIONAL_FUZZY_LANGUAGE.search(artist)
        ):
            return None
        return _RequestedTrackIdentity(title=title, artist=artist)

    artist_title = _ARTIST_TITLE.fullmatch(value)
    if artist_title:
        artist, title = (_clean_identity_part(part) for part in artist_title.groups())
        if not artist or not title or _FUZZY_LANGUAGE.search(value):
            return None
        return _RequestedTrackIdentity(title=title, artist=artist)

    title = _clean_identity_part(value)
    # A short bare title is supported for the existing "Add Teardrop" flow,
    # but descriptive or collection language is not proof of a concrete track.
    if (
        not title
        or len(title.split()) > 12
        or _FUZZY_LANGUAGE.search(title)
        or _GENERIC_TRACK_DESCRIPTION.search(title)
    ):
        return None
    return _RequestedTrackIdentity(title=title, artist=None)


def _clean_identity_part(value: str) -> str:
    return value.strip(" \t\"'\u201c\u201d\u2018\u2019:;,.!")


def _strip_source_qualifiers(value: str) -> str:
    """Remove only recognized trailing acquisition-provider constraints.

    Apply the suffix patterns repeatedly so requests such as ``from
    SoundCloud or via Bandcamp, not from YouTube`` retain the authoritative
    title/artist while provider policy is parsed independently.
    """

    previous = None
    while value != previous:
        previous = value
        for pattern in _SOURCE_QUALIFIER_SUFFIXES:
            value = pattern.sub("", value).strip()
    return value


def _has_collection_origin(request: Request, track: RequestTrack) -> bool:
    if getattr(request, "input_kind", None) == "media_collection_url":
        return True
    try:
        provenance = json.loads(getattr(track, "metadata_provenance_json", None) or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(
        isinstance(provenance, dict)
        and provenance.get("source") == "validated_direct_collection_metadata"
    )


def _has_verified_automatic_association(track: RequestTrack) -> bool:
    try:
        provenance = json.loads(track.metadata_provenance_json or "{}")
    except (AttributeError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(provenance, dict):
        return False
    score = provenance.get("score")
    if not (
        provenance.get("automatic_association") is True
        and provenance.get("recording_mbid") == track.recording_mbid
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
    ):
        return False
    if provenance.get("source") == "musicbrainz_search_recordings":
        return score >= 88
    model_confidence = provenance.get("model_confidence")
    return bool(
        provenance.get("source") == "openai_canonical_match"
        and score >= 75
        and isinstance(model_confidence, (int, float))
        and not isinstance(model_confidence, bool)
        and model_confidence >= 0.90
    )
