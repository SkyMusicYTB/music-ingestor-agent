from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.services.recording_versions import (
    canonical_recording_version_labels,
    recording_version_evidence,
)

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
    EXTENDED = "extended"
    NIGHTCORE = "nightcore"


_KINDS_BY_CANONICAL_LABEL: tuple[tuple[str, VersionKind], ...] = (
    ("live", VersionKind.LIVE),
    ("acoustic", VersionKind.ACOUSTIC),
    ("remix", VersionKind.REMIX),
    ("radio edit", VersionKind.RADIO_EDIT),
    ("remaster", VersionKind.REMASTER),
    ("demo", VersionKind.DEMO),
    ("instrumental", VersionKind.INSTRUMENTAL),
    ("sped up", VersionKind.SPED_UP),
    ("slowed", VersionKind.SLOWED),
    ("extended", VersionKind.EXTENDED),
    ("cover", VersionKind.COVER),
    ("karaoke", VersionKind.KARAOKE),
    ("nightcore", VersionKind.NIGHTCORE),
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
        return next(kind for _label, kind in _KINDS_BY_CANONICAL_LABEL if kind in self.kinds)

    @property
    def signature(self) -> str:
        if not self.kinds:
            return VersionKind.STUDIO.value
        ordered = [kind.value for _label, kind in _KINDS_BY_CANONICAL_LABEL if kind in self.kinds]
        return "+".join(ordered)


class VersionClassifier:
    def classify(self, *recording_values: str | None) -> VersionClassification:
        """Classify recording titles using context-aware qualifier evidence."""

        return self.classify_recording(*recording_values)

    def classify_recording(
        self,
        *recording_values: str | None,
        explicit_version: str | None = None,
    ) -> VersionClassification:
        samples = recording_version_evidence(*recording_values)
        labels = set(canonical_recording_version_labels(*samples, explicit_version))
        return VersionClassification(
            frozenset(kind for label, kind in _KINDS_BY_CANONICAL_LABEL if label in labels)
        )

    def classify_signature(self, value: str | None) -> VersionClassification:
        if not value:
            return VersionClassification(frozenset())
        labels = set(canonical_recording_version_labels(value))
        return VersionClassification(
            frozenset(kind for label, kind in _KINDS_BY_CANONICAL_LABEL if label in labels)
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
        DEFAULT_VERSION_CLASSIFIER.classify_signature(requested),
        DEFAULT_VERSION_CLASSIFIER.classify_signature(candidate),
    )
