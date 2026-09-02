from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.sources.identities import UploaderRelationship
from app.sources.models import SourceCandidate, SourceIntent, SourcePolicy
from app.sources.provider_metadata import resolve_provider_recording_metadata
from app.sources.providers import provider_capability
from app.sources.versions import (
    DEFAULT_VERSION_CLASSIFIER,
    VersionClassification,
    normalize_match_text,
)

CANONICAL_MATCH_WEIGHT = 0.45
VERSION_MATCH_WEIGHT = 0.20
DURATION_COMPATIBILITY_WEIGHT = 0.15
AUDIO_AVAILABILITY_QUALITY_WEIGHT = 0.08
UPLOADER_RELATIONSHIP_WEIGHT = 0.05
PROVIDER_RELIABILITY_WEIGHT = 0.05
PROVIDER_PREFERENCE_WEIGHT = 0.02

_OFFICIAL_SUFFIX_RE = re.compile(
    r"\s*[\[(]?(?:official\s+(?:music\s+)?(?:audio|video)|lyric(?:s|\s+video)?|"
    r"provided\s+to\s+youtube)[\])]?(?:\s+(?:hd|4k))?\s*$",
    re.I,
)
_BONUS_RELATIONSHIPS = frozenset(
    {
        UploaderRelationship.OFFICIAL_ARTIST,
        UploaderRelationship.OFFICIAL_LABEL,
        UploaderRelationship.TOPIC,
    }
)


@dataclass(frozen=True, slots=True)
class SourceScoreComponents:
    canonical_match: float
    requested_version: float
    duration_compatibility: float
    audio_availability_quality: float
    uploader_relationship: float
    provider_reliability: float
    provider_preference: float

    @property
    def weighted_total(self) -> float:
        return (
            self.canonical_match * CANONICAL_MATCH_WEIGHT
            + self.requested_version * VERSION_MATCH_WEIGHT
            + self.duration_compatibility * DURATION_COMPATIBILITY_WEIGHT
            + self.audio_availability_quality * AUDIO_AVAILABILITY_QUALITY_WEIGHT
            + self.uploader_relationship * UPLOADER_RELATIONSHIP_WEIGHT
            + self.provider_reliability * PROVIDER_RELIABILITY_WEIGHT
            + self.provider_preference * PROVIDER_PREFERENCE_WEIGHT
        )


@dataclass(frozen=True, slots=True)
class RankedSource:
    candidate: SourceCandidate
    score: float
    components: SourceScoreComponents
    version_match: bool
    duration_compatible: bool
    canonical_exact: bool
    contradiction_codes: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceGroup:
    group_id: str
    candidates: tuple[SourceCandidate, ...]


@dataclass(frozen=True, slots=True)
class RankedSourceGroup:
    group_id: str
    ranked: tuple[RankedSource, ...]

    @property
    def best(self) -> RankedSource:
        return self.ranked[0]


def rank_sources(
    intent: SourceIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    policy: SourcePolicy | None = None,
) -> tuple[RankedSource, ...]:
    active_policy = policy or SourcePolicy()
    unique: dict[str, SourceCandidate] = {}
    for candidate in candidates:
        key = candidate.identity.stable_key
        previous = unique.get(key)
        if previous is not None and previous != candidate:
            raise ValueError(f"conflicting duplicate source identity: {key}")
        unique[key] = candidate
    if len(unique) > active_policy.max_candidates:
        raise ValueError("source candidates exceed the configured finite limit")
    ranked = [_rank_one(intent, candidate, active_policy) for candidate in unique.values()]
    ranked.sort(key=lambda item: (-item.score, item.candidate.identity.stable_key))
    return tuple(ranked)


def _rank_one(
    intent: SourceIntent,
    candidate: SourceCandidate,
    policy: SourcePolicy,
) -> RankedSource:
    expected_artist = normalize_match_text(intent.artist)
    expected_title = _clean_title(intent.title)
    candidate_artist, candidate_title = candidate_track_fields(candidate)
    artist_score = _similarity(expected_artist, candidate_artist)
    title_score = _similarity(expected_title, candidate_title)
    canonical_score = (artist_score + title_score) / 2.0
    canonical_exact = bool(
        expected_artist
        and expected_title
        and candidate_artist == expected_artist
        and candidate_title == expected_title
    )

    requested_version = DEFAULT_VERSION_CLASSIFIER.classify(
        intent.title,
        intent.requested_version,
    )
    candidate_version = DEFAULT_VERSION_CLASSIFIER.classify(
        candidate.title,
        candidate.track,
        candidate.version,
    )
    version_match = DEFAULT_VERSION_CLASSIFIER.compatible(requested_version, candidate_version)
    contradictions = list(
        DEFAULT_VERSION_CLASSIFIER.contradictions(requested_version, candidate_version)
    )
    if candidate_artist and expected_artist and _other_artist(candidate_artist, expected_artist):
        contradictions.append("other_artist")

    duration_score, duration_compatible = _duration_component(intent, candidate, policy)
    preference_score = _provider_preference_score(candidate, policy)
    components = SourceScoreComponents(
        canonical_match=canonical_score,
        requested_version=1.0 if version_match else 0.0,
        duration_compatibility=duration_score,
        audio_availability_quality=(candidate.audio_quality if candidate.audio_available else 0.0),
        uploader_relationship=(
            1.0 if candidate.uploader_relationship in _BONUS_RELATIONSHIPS else 0.0
        ),
        provider_reliability=provider_capability(candidate.provider).reliability,
        provider_preference=preference_score,
    )
    reasons = (
        f"canonical={components.canonical_match:.3f}*{CANONICAL_MATCH_WEIGHT:.2f}",
        f"version={components.requested_version:.3f}*{VERSION_MATCH_WEIGHT:.2f}",
        f"duration={components.duration_compatibility:.3f}*{DURATION_COMPATIBILITY_WEIGHT:.2f}",
        f"audio={components.audio_availability_quality:.3f}*"
        f"{AUDIO_AVAILABILITY_QUALITY_WEIGHT:.2f}",
        f"uploader={components.uploader_relationship:.3f}*{UPLOADER_RELATIONSHIP_WEIGHT:.2f}",
        f"reliability={components.provider_reliability:.3f}*{PROVIDER_RELIABILITY_WEIGHT:.2f}",
        f"preference={components.provider_preference:.3f}*{PROVIDER_PREFERENCE_WEIGHT:.2f}",
    )
    return RankedSource(
        candidate=candidate,
        score=min(1.0, max(0.0, components.weighted_total)),
        components=components,
        version_match=version_match,
        duration_compatible=duration_compatible,
        canonical_exact=canonical_exact,
        contradiction_codes=tuple(sorted(set(contradictions))),
        reasons=reasons,
    )


def candidate_track_fields(candidate: SourceCandidate) -> tuple[str, str]:
    fields = resolve_provider_recording_metadata(
        {
            "artist": candidate.artist,
            "track": candidate.track,
            "title": candidate.title,
            "uploader": candidate.uploader_name,
        },
        provider_artist_may_be_provenance=candidate.artist_source in {None, "creator"},
    )
    return normalize_match_text(fields.artist or ""), _clean_title(fields.title)


def _clean_title(value: str | None) -> str:
    if not value:
        return ""
    return normalize_match_text(_OFFICIAL_SUFFIX_RE.sub("", value).strip())


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _other_artist(candidate_artist: str, expected_artist: str) -> bool:
    if candidate_artist == expected_artist:
        return False
    padded_candidate = f" {candidate_artist} "
    padded_expected = f" {expected_artist} "
    if padded_expected in padded_candidate or padded_candidate in padded_expected:
        return False
    return _similarity(candidate_artist, expected_artist) < 0.6


def _duration_component(
    intent: SourceIntent,
    candidate: SourceCandidate,
    policy: SourcePolicy,
) -> tuple[float, bool]:
    if intent.duration_seconds is None or candidate.duration_seconds is None:
        return 0.0, False
    tolerance = max(
        policy.duration_tolerance_seconds,
        intent.duration_seconds * policy.duration_tolerance_ratio,
    )
    difference = abs(intent.duration_seconds - candidate.duration_seconds)
    if difference <= tolerance:
        return 1.0, True
    return max(0.0, 1.0 - (difference - tolerance) / tolerance), False


def _provider_preference_score(candidate: SourceCandidate, policy: SourcePolicy) -> float:
    try:
        index = policy.provider_preference.index(candidate.provider)
    except ValueError:
        return 0.0
    count = len(policy.provider_preference)
    return (count - index) / count if count else 0.0


def sources_equivalent(
    left: SourceCandidate,
    right: SourceCandidate,
    *,
    duration_tolerance_seconds: float = 10.0,
    duration_tolerance_ratio: float = 0.05,
) -> bool:
    if duration_tolerance_seconds < 0 or duration_tolerance_ratio < 0:
        raise ValueError("duration tolerances cannot be negative")
    left_artist, left_title = candidate_track_fields(left)
    right_artist, right_title = candidate_track_fields(right)
    if (
        not left_artist
        or not left_title
        or (left_artist, left_title)
        != (
            right_artist,
            right_title,
        )
    ):
        return False
    if _candidate_version(left).kinds != _candidate_version(right).kinds:
        return False
    if left.duration_seconds is None or right.duration_seconds is None:
        return left.duration_seconds is None and right.duration_seconds is None
    tolerance = max(
        duration_tolerance_seconds,
        min(left.duration_seconds, right.duration_seconds) * duration_tolerance_ratio,
    )
    return abs(left.duration_seconds - right.duration_seconds) <= tolerance


def group_equivalent_sources(
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    duration_tolerance_seconds: float = 10.0,
    duration_tolerance_ratio: float = 0.05,
) -> tuple[SourceGroup, ...]:
    ordered = sorted(candidates, key=lambda candidate: candidate.identity.stable_key)
    groups: list[list[SourceCandidate]] = []
    for candidate in ordered:
        group = next(
            (
                current
                for current in groups
                if sources_equivalent(
                    current[0],
                    candidate,
                    duration_tolerance_seconds=duration_tolerance_seconds,
                    duration_tolerance_ratio=duration_tolerance_ratio,
                )
            ),
            None,
        )
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    return tuple(
        SourceGroup(group_id=_group_id(group), candidates=tuple(group)) for group in groups
    )


def group_ranked_sources(
    ranked: list[RankedSource] | tuple[RankedSource, ...],
) -> tuple[RankedSourceGroup, ...]:
    by_key = {item.candidate.identity.stable_key: item for item in ranked}
    groups = group_equivalent_sources(tuple(item.candidate for item in ranked))
    result = [
        RankedSourceGroup(
            group_id=group.group_id,
            ranked=tuple(
                sorted(
                    (by_key[candidate.identity.stable_key] for candidate in group.candidates),
                    key=lambda item: (-item.score, item.candidate.identity.stable_key),
                )
            ),
        )
        for group in groups
    ]
    result.sort(key=lambda group: (-group.best.score, group.group_id))
    return tuple(result)


def _candidate_version(candidate: SourceCandidate) -> VersionClassification:
    return DEFAULT_VERSION_CLASSIFIER.classify(candidate.title, candidate.track, candidate.version)


def _group_id(candidates: list[SourceCandidate]) -> str:
    material = "\x1f".join(sorted(candidate.identity.stable_key for candidate in candidates))
    return f"grp_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
