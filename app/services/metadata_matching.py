from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from rapidfuzz.fuzz import ratio, token_set_ratio

from app.services.artist_credits import artist_credit_similarity, structured_artists
from app.services.duplicates import recording_version_signature

_SPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_EDITION_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "compilation",
        re.compile(r"\b(compilation|greatest hits|best of|various artists)\b", re.I),
        12,
    ),
    ("deluxe", re.compile(r"\b(deluxe|expanded|anniversary(?: edition)?)\b", re.I), 8),
    ("remaster", re.compile(r"\b(re)?master(?:ed)?\b", re.I), 6),
    ("reissue", re.compile(r"\breissue(?:d)?\b", re.I), 6),
    ("soundtrack", re.compile(r"\b(?:soundtrack|original motion picture)\b", re.I), 8),
    ("tribute", re.compile(r"\b(?:tribute|karaoke)\b", re.I), 15),
    ("live_event", re.compile(r"\blive\s+(?:compilation|festival|event)\b", re.I), 8),
)
AssociationDecision = Literal["auto", "review", "reject"]


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = _PUNCT_RE.sub(" ", without_marks.casefold().replace("&", " and "))
    return _SPACE_RE.sub(" ", normalized).strip()


def release_edition_signature(*values: str | None) -> tuple[str, ...]:
    """Classify release packaging separately from the recorded performance."""

    combined = " ".join(value for value in values if value)
    return tuple(label for label, pattern, _amount in _EDITION_PATTERNS if pattern.search(combined))


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
    artists: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        disambiguation = self.raw.get("disambiguation") if self.raw else None
        return recording_version_signature(
            recording_title=self.title,
            recording_disambiguation=(
                str(disambiguation) if isinstance(disambiguation, str) else None
            ),
        )


@dataclass(frozen=True, slots=True)
class MatchResult:
    candidate: MetadataCandidate
    score: float
    decision: AssociationDecision
    lead: float | None
    reasons: tuple[str, ...]
    contradiction_codes: tuple[str, ...] = ()

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
        album_is_explicit: bool = False,
        version_is_explicit: bool = False,
        artists: tuple[str, ...] = (),
        requested_isrc: str | None = None,
        candidates: Iterable[MetadataCandidate],
        limit: int = 10,
    ) -> list[MatchResult]:
        query_version = recording_version_signature(
            explicit_version=requested_version if version_is_explicit else None,
            recording_title=title,
        )
        results: list[MatchResult] = []
        for candidate in candidates:
            contradiction_codes: list[str] = []
            title_score = _similarity(title, candidate.title)
            artist_score = artist_credit_similarity(
                artist, candidate.artist, left_artists=artists, right_artists=candidate.artists
            )
            # Fuzzy similarity is useful for ordering review choices, but it must
            # not authorize dropping or adding a required collaborator.  The
            # structured-credit comparator deliberately caps subset/superset
            # matches below this boundary.
            if artist_score < 0.95:
                contradiction_codes.append("artist_credit_mismatch")
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
            if version_is_explicit and query_version != candidate.version:
                contradiction_codes.append("explicit_version_mismatch")
            if album_is_explicit:
                if not candidate.album:
                    contradiction_codes.append("explicit_album_missing")
                elif normalize_text(album) != normalize_text(candidate.album):
                    contradiction_codes.append("explicit_album_mismatch")
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
            if requested_isrc and candidate.raw:
                isrcs = candidate.raw.get("isrcs")
                if isinstance(isrcs, list) and requested_isrc.upper() in isrcs:
                    reasons.append("matching_isrc")
            # Release-edition allowances come only from trusted, explicit user
            # constraints. A model-proposed album or a recording title must not
            # turn a compilation/remaster into requested release context.
            requested_edition_context = " ".join(
                value
                for value in (
                    requested_version if version_is_explicit else None,
                    album if album_is_explicit else None,
                )
                if value
            )
            penalty, penalty_reasons = _edition_penalty(
                requested=requested_edition_context,
                candidate=candidate.album or "",
            )
            if query_version != candidate.version and version_is_explicit:
                penalty += 12
                penalty_reasons.append(f"explicit-version-mismatch:{candidate.version}=-12")
            elif query_version != candidate.version and candidate.version != "studio":
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
                    contradiction_codes=tuple(contradiction_codes),
                )
            )
        # An explicit user constraint outranks a numerically stronger but
        # contradictory candidate. This still keeps contradictory candidates
        # available for one exceptional review when no compatible result exists.
        results.sort(
            key=lambda result: (
                bool(result.contradiction_codes),
                -result.score,
                _candidate_key(result.candidate),
            )
        )
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
        recording_id = _valid_mbid(value.get("id"))
        title = value.get("title")
        if not recording_id or not artist or not isinstance(title, str) or not title.strip():
            continue
        releases = value.get("releases", [])
        release = releases[0] if isinstance(releases, list) and releases else {}
        if not isinstance(release, Mapping):
            release = {}
        release_group = release.get("release-group", {})
        if not isinstance(release_group, Mapping):
            release_group = {}
        date = str(value.get("first-release-date") or "")
        candidates.append(
            select_sensible_release(
                MetadataCandidate(
                    artist=artist,
                    title=title.strip()[:500],
                    album=str(release.get("title")) if release.get("title") else None,
                    year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
                    duration_seconds=_recording_duration(value.get("length")),
                    recording_mbid=recording_id,
                    release_mbid=_valid_mbid(release.get("id")),
                    release_group_mbid=(_valid_mbid(release_group.get("id"))),
                    source="musicbrainz",
                    raw=value,
                    artists=_individual_artist_credit(credits),
                ),
                requested_album=None,
            )
        )
    return candidates


def select_sensible_release(
    candidate: MetadataCandidate, *, requested_album: str | None
) -> MetadataCandidate:
    """Choose canonical release packaging without changing recording identity.

    MusicBrainz search ordering is not canonical-release ordering. Prefer an
    explicitly requested release, then an official standard album/single, and
    only then compilations, deluxe editions, remasters, or reissues.
    """

    raw = candidate.raw
    if not isinstance(raw, Mapping):
        return candidate
    releases = raw.get("releases")
    if not isinstance(releases, list):
        return candidate
    valid = [
        release
        for release in releases[:50]
        if isinstance(release, Mapping) and _valid_mbid(release.get("id")) is not None
    ]
    if not valid:
        return candidate
    requested = normalize_text(requested_album)

    def release_key(release: Mapping[str, Any]) -> tuple[object, ...]:
        title = str(release.get("title") or "")
        release_group = release.get("release-group")
        group = release_group if isinstance(release_group, Mapping) else {}
        secondary = group.get("secondary-types")
        secondary_types = secondary if isinstance(secondary, list) else []
        status = normalize_text(str(release.get("status") or ""))
        primary = normalize_text(str(group.get("primary-type") or ""))
        date = str(release.get("date") or "")
        year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 9999
        normalized_title = normalize_text(title)
        edition_flags = release_edition_signature(
            title, *(str(item) for item in secondary_types[:10])
        )
        explicit_album = int(bool(requested) and normalized_title == requested)
        official = int(status == "official")
        standard = int(not edition_flags)
        canonical_type = 2 if primary == "album" else 1 if primary == "single" else 0
        date_key = (
            year,
            int(date[5:7]) if len(date) >= 7 and date[5:7].isdigit() else 13,
            int(date[8:10]) if len(date) >= 10 and date[8:10].isdigit() else 32,
        )
        return (
            -explicit_album,
            -official,
            -standard,
            date_key,
            -canonical_type,
            normalized_title,
        )

    selected = min(valid, key=release_key)
    group_value = selected.get("release-group")
    release_group = group_value if isinstance(group_value, Mapping) else {}
    selected_release_mbid = _valid_mbid(selected.get("id"))
    selected_release_group_mbid = _valid_mbid(release_group.get("id"))
    # A candidate may already carry IDs from a different release selected during
    # an earlier bounded search. Never combine that prior release group's ID with
    # the newly selected release. The same-release fallback is safe when a later
    # provider response omitted details that were already validated locally.
    same_release = selected_release_mbid == candidate.release_mbid
    date = str(selected.get("date") or raw.get("first-release-date") or "")
    year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else candidate.year
    return replace(
        candidate,
        album=str(selected.get("title")) if selected.get("title") else candidate.album,
        year=year,
        release_mbid=selected_release_mbid,
        release_group_mbid=(
            selected_release_group_mbid or (candidate.release_group_mbid if same_release else None)
        ),
    )


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
    next_compatible = next(
        (item for item in results[1:] if not item.contradiction_codes),
        None,
    )
    lead = results[0].score - next_compatible.score if next_compatible is not None else None
    top_decision: AssociationDecision = (
        "auto"
        if (
            results[0].score >= 88
            and (lead is None or lead >= 8)
            and not results[0].contradiction_codes
        )
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
    return set(release_edition_signature(value))


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


def _individual_artist_credit(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names: list[object] = []
    for credit in value[:8]:
        if not isinstance(credit, Mapping):
            continue
        artist = credit.get("artist")
        names.append(
            credit.get("name") or (artist.get("name") if isinstance(artist, Mapping) else None)
        )
    return structured_artists(names)


def _valid_mbid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _recording_duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        milliseconds = float(value)
    except ValueError:
        return None
    return milliseconds / 1000.0 if 0 < milliseconds <= 86_400_000 else None


def _apple_year(value: Any) -> int | None:
    text = str(value or "")
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None
