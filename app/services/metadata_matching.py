from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from rapidfuzz.fuzz import ratio, token_set_ratio

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("live", re.compile(r"\b(live|concert|unplugged live)\b", re.I)),
    ("remix", re.compile(r"\b(remix|mix by|club mix|rework)\b", re.I)),
    ("acoustic", re.compile(r"\b(acoustic|unplugged)\b", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b", re.I)),
    ("karaoke", re.compile(r"\bkaraoke\b", re.I)),
    ("demo", re.compile(r"\bdemo\b", re.I)),
    ("radio-edit", re.compile(r"\bradio (edit|version)\b", re.I)),
    ("remaster", re.compile(r"\b(re)?master(?:ed)?\b", re.I)),
)
_EDITION_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "compilation",
        re.compile(r"\b(compilation|greatest hits|best of|various artists)\b", re.I),
        12,
    ),
    ("deluxe", re.compile(r"\b(deluxe|expanded|anniversary edition)\b", re.I), 8),
    ("remaster", re.compile(r"\b(re)?master(?:ed)?\b", re.I), 6),
)
AssociationDecision = Literal["auto", "review", "reject"]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = _PUNCT_RE.sub(" ", without_marks.casefold().replace("&", " and "))
    return _SPACE_RE.sub(" ", normalized).strip()


def version_signature(*values: str | None) -> str:
    combined = " ".join(value for value in values if value)
    versions = [name for name, pattern in _VERSION_PATTERNS if pattern.search(combined)]
    return "+".join(versions) if versions else "studio"


@dataclass(frozen=True, slots=True)
class MetadataCandidate:
    artist: str
    title: str
    album: str | None = None
    year: int | None = None
    duration_seconds: float | None = None
    recording_mbid: str | None = None
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    source: str = "unknown"
    raw: Mapping[str, Any] | None = None

    @property
    def version(self) -> str:
        return version_signature(self.title, self.album)


@dataclass(frozen=True, slots=True)
class MatchResult:
    candidate: MetadataCandidate
    score: float
    decision: AssociationDecision
    lead: float | None
    reasons: tuple[str, ...]

    @property
    def confidence(self) -> str:
        return self.decision


class MetadataMatcher:
    """Locked recording matcher: 35/35/15/10/5 plus edition penalties."""

    def rank(
        self,
        *,
        artist: str,
        title: str,
        album: str | None = None,
        duration_seconds: float | None = None,
        requested_version: str | None = None,
        requested_year: int | None = None,
        candidates: Iterable[MetadataCandidate],
        limit: int = 10,
    ) -> list[MatchResult]:
        query_version = version_signature(requested_version, title, album)
        results: list[MatchResult] = []
        for candidate in candidates:
            title_score = _similarity(title, candidate.title)
            artist_score = _similarity(artist, candidate.artist)
            duration_score, duration_reason = _duration_score(
                duration_seconds, candidate.duration_seconds
            )
            album_score = (
                _similarity(album, candidate.album)
                if album and candidate.album
                else 0.5
                if album or candidate.album
                else 1.0
            )
            version_score = 1.0 if query_version == candidate.version else 0.0
            evidence_scores = [version_score]
            if requested_year is not None:
                evidence_scores.append(_year_score(requested_year, candidate.year))
            version_date_score = sum(evidence_scores) / len(evidence_scores)
            score = (
                title_score * 35
                + artist_score * 35
                + duration_score * 15
                + album_score * 10
                + version_date_score * 5
            )
            reasons = [
                f"title={title_score * 35:.1f}/35",
                f"artist={artist_score * 35:.1f}/35",
                f"duration={duration_score * 15:.1f}/15 ({duration_reason})",
                f"album={album_score * 10:.1f}/10",
                f"version_date={version_date_score * 5:.1f}/5",
            ]
            penalty, penalty_reasons = _edition_penalty(
                requested=" ".join(value for value in (requested_version, title, album) if value),
                candidate=" ".join(value for value in (candidate.title, candidate.album) if value),
            )
            if query_version != candidate.version and candidate.version != "studio":
                penalty += 12
                penalty_reasons.append(f"unrequested-version:{candidate.version}=-12")
            score = max(0.0, min(100.0, score - penalty))
            results.append(
                MatchResult(
                    candidate=candidate,
                    score=score,
                    decision="reject",
                    lead=None,
                    reasons=tuple([*reasons, *penalty_reasons]),
                )
            )
        results.sort(key=lambda result: (-result.score, _candidate_key(result.candidate)))
        return _classify_recordings(results[: max(0, limit)])


@dataclass(frozen=True, slots=True)
class ReleaseMetadataCandidate:
    album: str
    status: str | None
    primary_type: str | None
    recording_fit: float
    version_fit: float
    duration_fit: float
    track_placement_fit: float
    original_year: int | None
    edition: str | None = None
    release_mbid: str | None = None
    source: str = "musicbrainz"


@dataclass(frozen=True, slots=True)
class ReleaseMatchResult:
    candidate: ReleaseMetadataCandidate
    score: float
    decision: AssociationDecision
    lead: float | None
    reasons: tuple[str, ...]


class ReleaseMetadataMatcher:
    """Locked release matcher: 35/15/10/15/10/10/5 plus edition penalties."""

    def rank(
        self,
        *,
        requested_album: str,
        requested_primary_type: str,
        requested_version: str | None,
        requested_year: int | None,
        candidates: Iterable[ReleaseMetadataCandidate],
        limit: int = 10,
    ) -> list[ReleaseMatchResult]:
        results: list[ReleaseMatchResult] = []
        for candidate in candidates:
            album_score = _similarity(requested_album, candidate.album)
            official_score = 1.0 if normalize_text(candidate.status) == "official" else 0.0
            type_score = _similarity(requested_primary_type, candidate.primary_type)
            recording_version_score = (
                _bounded_score(candidate.recording_fit) + _bounded_score(candidate.version_fit)
            ) / 2
            duration_placement_score = (
                _bounded_score(candidate.duration_fit)
                + _bounded_score(candidate.track_placement_fit)
            ) / 2
            date_score = _year_score(requested_year, candidate.original_year)
            edition_text = " ".join(
                value for value in (candidate.album, candidate.edition) if value
            )
            edition_score = (
                1.0
                if _edition_flags(edition_text)
                <= _edition_flags(
                    " ".join(value for value in (requested_album, requested_version) if value)
                )
                else 0.0
            )
            score = (
                album_score * 35
                + official_score * 15
                + type_score * 10
                + recording_version_score * 15
                + duration_placement_score * 10
                + date_score * 10
                + edition_score * 5
            )
            penalty, penalty_reasons = _edition_penalty(
                requested=" ".join(
                    value for value in (requested_album, requested_version) if value
                ),
                candidate=edition_text,
            )
            score = max(0.0, min(100.0, score - penalty))
            results.append(
                ReleaseMatchResult(
                    candidate=candidate,
                    score=score,
                    decision="reject",
                    lead=None,
                    reasons=(
                        f"album={album_score * 35:.1f}/35",
                        f"official={official_score * 15:.1f}/15",
                        f"primary_type={type_score * 10:.1f}/10",
                        f"recording_version={recording_version_score * 15:.1f}/15",
                        f"duration_placement={duration_placement_score * 10:.1f}/10",
                        f"original_date={date_score * 10:.1f}/10",
                        f"edition={edition_score * 5:.1f}/5",
                        *penalty_reasons,
                    ),
                )
            )
        results.sort(
            key=lambda result: (
                -result.score,
                result.candidate.release_mbid or "",
                normalize_text(result.candidate.album),
            )
        )
        return _classify_releases(results[: max(0, limit)])


def candidates_from_musicbrainz(payload: Mapping[str, Any]) -> list[MetadataCandidate]:
    values = payload.get("recordings", [])
    if not isinstance(values, list):
        return []
    candidates: list[MetadataCandidate] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        credits = value.get("artist-credit", [])
        artist = _artist_credit(credits)
        releases = value.get("releases", [])
        release = releases[0] if isinstance(releases, list) and releases else {}
        if not isinstance(release, Mapping):
            release = {}
        release_group = release.get("release-group", {})
        if not isinstance(release_group, Mapping):
            release_group = {}
        date = str(value.get("first-release-date") or "")
        candidates.append(
            MetadataCandidate(
                artist=artist,
                title=str(value.get("title") or ""),
                album=str(release.get("title")) if release.get("title") else None,
                year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
                duration_seconds=(float(value["length"]) / 1000 if value.get("length") else None),
                recording_mbid=str(value.get("id")) if value.get("id") else None,
                release_mbid=str(release.get("id")) if release.get("id") else None,
                release_group_mbid=(
                    str(release_group.get("id")) if release_group.get("id") else None
                ),
                source="musicbrainz",
                raw=value,
            )
        )
    return candidates


def candidates_from_apple(payload: Mapping[str, Any]) -> list[MetadataCandidate]:
    values = payload.get("results", [])
    if not isinstance(values, list):
        return []
    return [
        MetadataCandidate(
            artist=str(value.get("artistName") or ""),
            title=str(value.get("trackName") or ""),
            album=str(value.get("collectionName")) if value.get("collectionName") else None,
            year=_apple_year(value.get("releaseDate")),
            duration_seconds=(
                float(value["trackTimeMillis"]) / 1000 if value.get("trackTimeMillis") else None
            ),
            source="apple",
            raw=value,
        )
        for value in values
        if isinstance(value, Mapping) and value.get("kind") == "song"
    ]


def _classify_recordings(results: list[MatchResult]) -> list[MatchResult]:
    if not results:
        return []
    lead = results[0].score - results[1].score if len(results) > 1 else None
    top_decision: AssociationDecision = (
        "auto"
        if results[0].score >= 88 and (lead is None or lead >= 8)
        else "review"
        if results[0].score >= 70
        else "reject"
    )
    classified = [replace(results[0], decision=top_decision, lead=lead)]
    classified.extend(
        replace(item, decision="review" if item.score >= 70 else "reject") for item in results[1:]
    )
    return classified


def _classify_releases(results: list[ReleaseMatchResult]) -> list[ReleaseMatchResult]:
    if not results:
        return []
    lead = results[0].score - results[1].score if len(results) > 1 else None
    top_decision: AssociationDecision = (
        "auto"
        if results[0].score >= 88 and (lead is None or lead >= 8)
        else "review"
        if results[0].score >= 70
        else "reject"
    )
    classified = [replace(results[0], decision=top_decision, lead=lead)]
    classified.extend(
        replace(item, decision="review" if item.score >= 70 else "reject") for item in results[1:]
    )
    return classified


def _similarity(left: str | None, right: str | None) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return (
        max(
            ratio(left_normalized, right_normalized),
            token_set_ratio(left_normalized, right_normalized),
        )
        / 100
    )


def _duration_score(left: float | None, right: float | None) -> tuple[float, str]:
    if left is None or right is None:
        return 0.5, "unknown"
    difference = abs(left - right)
    if difference <= 2:
        return 1.0, f"delta={difference:.1f}s"
    if difference <= 8:
        return 0.8, f"delta={difference:.1f}s"
    if difference <= 20:
        return 0.4, f"delta={difference:.1f}s"
    return 0.0, f"delta={difference:.1f}s"


def _year_score(requested: int | None, candidate: int | None) -> float:
    if requested is None:
        return 1.0
    if candidate is None:
        return 0.5
    difference = abs(requested - candidate)
    return 1.0 if difference == 0 else 0.8 if difference == 1 else 0.4 if difference <= 3 else 0.0


def _edition_penalty(*, requested: str, candidate: str) -> tuple[float, list[str]]:
    requested_flags = _edition_flags(requested)
    penalty = 0.0
    reasons: list[str] = []
    for label, pattern, amount in _EDITION_PATTERNS:
        if pattern.search(candidate) and label not in requested_flags:
            penalty += amount
            reasons.append(f"unrequested-{label}=-{amount:.0f}")
    return penalty, reasons


def _edition_flags(value: str) -> set[str]:
    return {label for label, pattern, _amount in _EDITION_PATTERNS if pattern.search(value)}


def _bounded_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _candidate_key(candidate: MetadataCandidate) -> tuple[str, str, str]:
    return (
        candidate.recording_mbid or "",
        normalize_text(candidate.artist),
        normalize_text(candidate.title),
    )


def _artist_credit(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for credit in value:
        if not isinstance(credit, Mapping):
            continue
        name = credit.get("name")
        artist = credit.get("artist")
        if not name and isinstance(artist, Mapping):
            name = artist.get("name")
        if name:
            parts.append(str(name))
        join = credit.get("joinphrase")
        if join:
            parts.append(str(join))
    return "".join(parts).strip()


def _apple_year(value: Any) -> int | None:
    text = str(value or "")
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None
