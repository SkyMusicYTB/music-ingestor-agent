from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz.fuzz import ratio
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Track
from app.services.library_presence import library_presence
from app.services.recording_versions import (
    canonical_recording_version_labels,
    recording_version_evidence,
)

_SAFE_SUFFIX = re.compile(
    r"\s*[\[(]\s*(?:official\s+(?:music\s+)?video|official\s+audio|lyrics?|hd|4k)\s*[\])]\s*$",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)
# ``|`` is the durable serialized boundary and must never become part of a
# featured-artist credit. A plus sign can be part of a real artist credit, so it
# remains raw input for ``normalize_text`` rather than being truncated here.
_FEATURE = re.compile(r"\b(?:feat\.?|ft\.?)\s+([^][()\-\u2013\u2014|]+)", re.I)


def strip_provider_suffixes(value: str) -> str:
    result = unicodedata.normalize("NFKC", value).strip()
    while True:
        stripped = _SAFE_SUFFIX.sub("", result).strip()
        if stripped == result:
            return result
        result = stripped


def normalize_text(value: str) -> str:
    value = strip_provider_suffixes(value).casefold()
    value = _PUNCT.sub(" ", value)
    return " ".join(value.split())


def version_signature(*values: str | None) -> str:
    # Version values cross several bounded provider/model interfaces. Normalize
    # compound labels before classification so ``radio_edit`` and ``radio edit``
    # (and the equivalent sped-up spellings) cannot become distinct identities.
    combined = " ".join(value for value in values if value)
    found = list(canonical_recording_version_labels(combined))
    feature = _FEATURE.search(combined)
    if feature:
        featured = normalize_text(feature.group(1))[:100]
        if featured:
            found.append(f"feat {featured}")
    return "|".join(sorted(set(found))) or "studio"


def normalize_version_signature(value: str | None) -> str:
    """Return the one durable serialization used by comparisons and dedup keys.

    Both ``+`` and ``|`` are accepted as legacy/internal compound separators.
    The classifier extracts the bounded supported version vocabulary and emits
    stable, sorted pipe-separated labels.
    """

    return version_signature(value)


def recording_version_signature(
    *,
    explicit_version: str | None = None,
    recording_title: str | None = None,
    recording_disambiguation: str | None = None,
) -> str:
    """Classify recording evidence without accepting album/release text."""

    signature = version_signature(
        explicit_version,
        recording_disambiguation,
        *recording_version_evidence(recording_title),
    )
    found = [] if signature == "studio" else signature.split("|")
    feature = _FEATURE.search(recording_title or "")
    if feature:
        featured = normalize_text(feature.group(1))[:100]
        if featured:
            found.append(f"feat {featured}")
    return "|".join(sorted(set(found))) or "studio"


def versions_compatible(left: str | None, right: str | None) -> bool:
    return normalize_version_signature(left) == normalize_version_signature(right)


@dataclass(frozen=True)
class DuplicateCandidate:
    artist: str
    title: str
    version_signature: str = "studio"
    duration_seconds: float | None = None
    recording_mbid: str | None = None
    source_extractor: str | None = None
    source_id: str | None = None


@dataclass(frozen=True)
class DuplicateDecision:
    status: str
    track_id: str | None = None
    reason: str | None = None


class DuplicateDetector:
    def __init__(self, music_root: Path) -> None:
        self.music_root = music_root.resolve()

    def find(self, session: Session, candidate: DuplicateCandidate) -> DuplicateDecision:
        rows = self._candidate_rows(session, candidate)
        possible: Track | None = None
        for track in rows:
            if not self._still_exists(session, track):
                if library_presence(self.music_root, track.filepath) == "unreadable":
                    possible = possible or track
                continue
            compatible = versions_compatible(track.version_signature, candidate.version_signature)
            if (
                candidate.source_extractor
                and candidate.source_id
                and source_identity(track) == (candidate.source_extractor, candidate.source_id)
            ):
                return DuplicateDecision("owned", track.id, "same validated source")
            if candidate.recording_mbid and track.recording_mbid == candidate.recording_mbid:
                if compatible:
                    return DuplicateDecision("owned", track.id, "same MusicBrainz recording")
                possible = possible or track
                continue
            artist_score = ratio(normalize_text(track.artist), normalize_text(candidate.artist))
            title_score = ratio(normalize_text(track.title), normalize_text(candidate.title))
            duration_ok = self._duration_agrees(track.duration_seconds, candidate.duration_seconds)
            if artist_score == 100 and title_score == 100 and compatible:
                return DuplicateDecision("owned", track.id, "exact normalized artist/title")
            if artist_score >= 95 and title_score >= 97 and compatible and duration_ok:
                return DuplicateDecision("owned", track.id, "conservative fuzzy identity")
            if artist_score >= 88 and title_score >= 88 and duration_ok:
                possible = possible or track
        if possible is not None:
            return DuplicateDecision("possible", possible.id, "similar library track")
        return DuplicateDecision("none")

    def _candidate_rows(self, session: Session, candidate: DuplicateCandidate) -> list[Track]:
        predicates: list[ColumnElement[bool]] = [
            (
                (Track.artist_normalized == normalize_text(candidate.artist))
                & (Track.title_normalized == normalize_text(candidate.title))
            )
        ]
        if candidate.recording_mbid:
            predicates.append(Track.recording_mbid == candidate.recording_mbid)
        if candidate.source_extractor and candidate.source_id:
            predicates.append(
                (Track.source_extractor == candidate.source_extractor)
                & (Track.source_id == candidate.source_id)
            )
            valid_json = case(
                (func.json_valid(Track.provenance_json) == 1, Track.provenance_json), else_="{}"
            )
            predicates.append(
                (
                    func.json_extract(valid_json, "$.source_alias.extractor")
                    == candidate.source_extractor
                )
                & (func.json_extract(valid_json, "$.source_alias.id") == candidate.source_id)
            )
        direct = list(session.scalars(select(Track).where(Track.is_present, or_(*predicates))))
        if direct:
            return direct
        # Bounded fuzzy pool: matching normalized artist prefix or recent rows only.
        artist = normalize_text(candidate.artist)
        return list(
            session.scalars(
                select(Track)
                .where(Track.is_present, Track.artist_normalized.like(f"{artist[:80]}%"))
                .limit(100)
            )
        )

    def _still_exists(self, session: Session, track: Track) -> bool:
        presence = library_presence(self.music_root, track.filepath)
        if presence in {"missing", "unsafe"}:
            track.is_present = False
        return presence == "present"

    @staticmethod
    def _duration_agrees(left: float | None, right: float | None) -> bool:
        if left is None or right is None:
            return False
        return abs(left - right) <= max(4.0, max(left, right) * 0.02)


def source_identity(track: Track) -> tuple[str | None, str | None]:
    if track.source_extractor and track.source_id:
        return track.source_extractor, track.source_id
    try:
        value = json.loads(track.provenance_json or "{}")
    except ValueError:
        return None, None
    alias = value.get("source_alias") if isinstance(value, dict) else None
    if isinstance(alias, dict):
        extractor, source_id = alias.get("extractor"), alias.get("id")
        if (
            isinstance(extractor, str)
            and isinstance(source_id, str)
            and len(extractor) <= 40
            and len(source_id) <= 100
        ):
            return extractor, source_id
    return None, None
