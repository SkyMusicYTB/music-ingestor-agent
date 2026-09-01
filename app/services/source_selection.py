from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
_RISKY_VERSION_PHRASES = (
    "live",
    "cover",
    "karaoke",
    "instrumental",
    "remix",
    "mix",
    "sped up",
    "speed up",
    "slowed",
    "nightcore",
    "reaction",
    "tutorial",
    "8d audio",
)
_OFFICIAL_PHRASES = ("official audio", "official video", "provided to youtube")


@dataclass(frozen=True, slots=True)
class TrackIntent:
    artist: str
    title: str
    duration_seconds: float | None = None
    version_signature: str = "studio"


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    source_id: str
    url: str
    title: str
    channel: str | None
    duration_seconds: float | None
    extractor: str = "youtube"


@dataclass(frozen=True, slots=True)
class RankedSource:
    candidate: SourceCandidate
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    selected: SourceCandidate | None
    ranked: tuple[RankedSource, ...]
    needs_review: bool
    reason: str
    ambiguous: bool = False


def normalize_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _NON_WORD_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _contains_phrase(text: str, phrase: str) -> bool:
    padded = f" {text} "
    return f" {normalize_match_text(phrase)} " in padded


def rank_sources(
    intent: TrackIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    max_duration_seconds: int,
) -> tuple[RankedSource, ...]:
    if max_duration_seconds <= 0:
        raise ValueError("max_duration_seconds must be positive")
    expected_title = normalize_match_text(intent.title)
    expected_artist = normalize_match_text(intent.artist)
    requested_version = normalize_match_text(
        f"{intent.title} {intent.version_signature if intent.version_signature != 'studio' else ''}"
    )
    ranked: list[RankedSource] = []
    seen_ids: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = (candidate.extractor.casefold(), candidate.source_id)
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        if candidate.duration_seconds is not None and (
            candidate.duration_seconds <= 0 or candidate.duration_seconds > max_duration_seconds
        ):
            continue

        candidate_title = normalize_match_text(candidate.title)
        candidate_channel = normalize_match_text(candidate.channel or "")
        title_without_artist = candidate_title
        if expected_artist and candidate_title.startswith(f"{expected_artist} "):
            title_without_artist = candidate_title[len(expected_artist) + 1 :]
        provider_clean_title = title_without_artist
        for phrase in _OFFICIAL_PHRASES:
            provider_clean_title = provider_clean_title.replace(normalize_match_text(phrase), " ")
        provider_clean_title = _SPACE_RE.sub(" ", provider_clean_title).strip()
        title_score = max(
            _similarity(expected_title, candidate_title),
            _similarity(expected_title, title_without_artist),
            _similarity(expected_title, provider_clean_title),
        )
        artist_score = max(
            _similarity(expected_artist, candidate_channel),
            1.0 if _contains_phrase(candidate_title, expected_artist) else 0.0,
        )
        reasons = [f"title={title_score:.2f}", f"artist={artist_score:.2f}"]

        duration_score = 0.5
        if intent.duration_seconds is not None and candidate.duration_seconds is not None:
            difference = abs(intent.duration_seconds - candidate.duration_seconds)
            duration_score = max(0.0, 1.0 - difference / max(15.0, intent.duration_seconds * 0.15))
            reasons.append(f"duration_delta={difference:.1f}s")
        elif candidate.duration_seconds is not None:
            duration_score = 0.75

        official_bonus = 0.0
        if any(_contains_phrase(candidate_title, phrase) for phrase in _OFFICIAL_PHRASES):
            official_bonus += 0.04
            reasons.append("official-label")
        if candidate.channel and (
            candidate_channel.endswith(" topic") or "vevo" in candidate_channel
        ):
            official_bonus += 0.03
            reasons.append("official-channel")

        penalty = 0.0
        for phrase in _RISKY_VERSION_PHRASES:
            if _contains_phrase(candidate_title, phrase) and not _contains_phrase(
                requested_version, phrase
            ):
                penalty += 0.18 if phrase in {"mix", "live", "remix", "cover"} else 0.24
                reasons.append(f"unrequested-{phrase.replace(' ', '-')}")

        score = title_score * 0.56 + artist_score * 0.29 + duration_score * 0.10
        score = min(1.0, max(0.0, score + official_bonus - penalty))
        ranked.append(RankedSource(candidate=candidate, score=score, reasons=tuple(reasons)))
    ranked.sort(key=lambda item: (-item.score, item.candidate.source_id))
    return tuple(ranked)


def select_source(
    intent: TrackIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    max_duration_seconds: int,
    automatic_threshold: float = 0.84,
    ambiguity_margin: float = 0.08,
) -> SelectionDecision:
    ranked = rank_sources(intent, candidates, max_duration_seconds=max_duration_seconds)
    if not ranked:
        return SelectionDecision(
            selected=None,
            ranked=(),
            needs_review=True,
            reason="no eligible YouTube source was found",
        )
    best = ranked[0]
    if best.score < automatic_threshold:
        return SelectionDecision(
            selected=None,
            ranked=ranked,
            needs_review=True,
            reason=f"best source confidence {best.score:.2f} is below the automatic threshold",
        )
    if len(ranked) > 1 and best.score - ranked[1].score < ambiguity_margin:
        return SelectionDecision(
            selected=None,
            ranked=ranked,
            needs_review=True,
            reason="the leading sources are too close to choose automatically",
            ambiguous=True,
        )
    return SelectionDecision(
        selected=best.candidate,
        ranked=ranked,
        needs_review=False,
        reason=f"selected a source with confidence {best.score:.2f}",
    )
