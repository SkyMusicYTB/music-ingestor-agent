from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


class VersionKind(StrEnum):
    STUDIO = "studio"
    LIVE = "live"
    REMIX = "remix"
    COVER = "cover"
    KARAOKE = "karaoke"
    ACOUSTIC = "acoustic"
    INSTRUMENTAL = "instrumental"
    DEMO = "demo"
    RADIO_EDIT = "radio_edit"
    REMASTER = "remaster"
    SPED_UP = "sped_up"
    SLOWED = "slowed"
    NIGHTCORE = "nightcore"


_PATTERNS: tuple[tuple[VersionKind, re.Pattern[str]], ...] = (
    (VersionKind.LIVE, re.compile(r"\b(?:live|concert|live session)\b", re.I)),
    (
        VersionKind.REMIX,
        re.compile(r"\b(?:remix|rework|club mix|extended mix|radio mix|mix by)\b", re.I),
    ),
    (VersionKind.COVER, re.compile(r"\bcover(?:ed)?\b", re.I)),
    (VersionKind.KARAOKE, re.compile(r"\bkaraoke\b", re.I)),
    (VersionKind.ACOUSTIC, re.compile(r"\b(?:acoustic|unplugged)\b", re.I)),
    (VersionKind.INSTRUMENTAL, re.compile(r"\binstrumental\b", re.I)),
    (VersionKind.DEMO, re.compile(r"\bdemo\b", re.I)),
    (VersionKind.RADIO_EDIT, re.compile(r"\bradio\s+(?:edit|version)\b", re.I)),
    (VersionKind.REMASTER, re.compile(r"\b(?:re)?master(?:ed)?\b", re.I)),
    (VersionKind.SPED_UP, re.compile(r"\b(?:sped|speed)\s+up\b", re.I)),
    (VersionKind.SLOWED, re.compile(r"\bslowed(?:\s+down)?\b", re.I)),
    (VersionKind.NIGHTCORE, re.compile(r"\bnightcore\b", re.I)),
)
_CONTRADICTORY_IF_UNREQUESTED = frozenset(
    {VersionKind.LIVE, VersionKind.REMIX, VersionKind.COVER, VersionKind.KARAOKE}
)


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    normalized = _NON_WORD_RE.sub(" ", without_marks.casefold().replace("&", " and "))
    return _SPACE_RE.sub(" ", normalized).strip()


@dataclass(frozen=True, slots=True)
class VersionClassification:
    kinds: frozenset[VersionKind]

    @property
    def primary(self) -> VersionKind:
        if not self.kinds:
            return VersionKind.STUDIO
        return next(kind for kind, _pattern in _PATTERNS if kind in self.kinds)

    @property
    def signature(self) -> str:
        if not self.kinds:
            return VersionKind.STUDIO.value
        ordered = [kind.value for kind, _pattern in _PATTERNS if kind in self.kinds]
        return "+".join(ordered)


class VersionClassifier:
    def classify(self, *values: str | None) -> VersionClassification:
        combined = " ".join(value for value in values if value)
        return VersionClassification(
            frozenset(kind for kind, pattern in _PATTERNS if pattern.search(combined))
        )

    def compatible(
        self,
        requested: VersionClassification,
        candidate: VersionClassification,
    ) -> bool:
        return requested.kinds == candidate.kinds

    def contradictions(
        self,
        requested: VersionClassification,
        candidate: VersionClassification,
    ) -> tuple[str, ...]:
        unrequested = (candidate.kinds - requested.kinds) & _CONTRADICTORY_IF_UNREQUESTED
        return tuple(f"unrequested_{kind.value}" for kind in sorted(unrequested, key=str))


DEFAULT_VERSION_CLASSIFIER = VersionClassifier()


def classify_version(*values: str | None) -> VersionClassification:
    return DEFAULT_VERSION_CLASSIFIER.classify(*values)


def versions_compatible(*, requested: str | None, candidate: str | None) -> bool:
    return DEFAULT_VERSION_CLASSIFIER.compatible(
        DEFAULT_VERSION_CLASSIFIER.classify(requested),
        DEFAULT_VERSION_CLASSIFIER.classify(candidate),
    )
