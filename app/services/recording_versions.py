"""Extract version evidence from recording fields without reclassifying ordinary titles."""

from __future__ import annotations

import re
import unicodedata

CANONICAL_RECORDING_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("live", re.compile(r"\b(?:live|concert|unplugged\s+live)\b", re.I)),
    ("acoustic", re.compile(r"\b(?:acoustic|unplugged)\b", re.I)),
    ("remix", re.compile(r"\b(?:remix|rework|club\s+mix|radio\s+mix|mix\s+by)\b", re.I)),
    ("radio edit", re.compile(r"\bradio[ _-]+(?:edit|version)\b", re.I)),
    ("remaster", re.compile(r"\bremaster(?:ed)?(?:\s+\d{4})?\b", re.I)),
    ("demo", re.compile(r"\bdemo\b", re.I)),
    ("instrumental", re.compile(r"\binstrumental\b", re.I)),
    ("sped up", re.compile(r"\b(?:sped|speed)[ _-]+up\b", re.I)),
    ("slowed", re.compile(r"\bslowed(?:\s+down|\s*[+&]\s*reverb)?\b", re.I)),
    ("extended", re.compile(r"\bextended(?:\s+(?:mix|version))?\b", re.I)),
    ("cover", re.compile(r"\bcover(?:ed)?(?:\s+version)?\b", re.I)),
    ("karaoke", re.compile(r"\bkaraoke\b", re.I)),
    ("nightcore", re.compile(r"\bnightcore\b", re.I)),
)

_BRACKETED = re.compile(r"[\[(](?P<value>[^\])]{1,100})[\])]")
_SEPARATOR_SUFFIX = re.compile(r"\s(?:-|\u2013|\u2014|:)\s(?P<value>[^\r\n]{1,100})$")
_WHOLE_VERSION = re.compile(
    r"^(?:live|remix|acoustic|cover|karaoke|instrumental|demo|radio[ _-]?edit|"
    r"remaster(?:ed)?(?:\s+\d{4})?|sped[ _-]?up|slowed(?:\s*[+&]\s*reverb)?|"
    r"extended(?:\s+(?:mix|version))?|nightcore)$",
    re.IGNORECASE,
)
_SUFFIX_VERSION = re.compile(
    r"^(?:live(?:\s+(?:at|from)\s+.{1,80})?|(?:.{1,60}\s+)?remix|rework|club\s+mix|"
    r"extended\s+(?:mix|version)|radio\s+(?:edit|version|mix)|mix\s+by\s+.{1,60}|"
    r"acoustic(?:\s+(?:version|recording|session))?|cover(?:\s+version)?|karaoke|"
    r"instrumental(?:\s+version)?|demo|remaster(?:ed)?(?:\s+\d{4})?|"
    r"sped[ -]?up|slowed(?:\s*[+&]\s*reverb)?|nightcore)$",
    re.IGNORECASE,
)
_TRAILING_MARKER = re.compile(
    r"(?P<value>\b(?:remix|rework|karaoke|cover|acoustic|instrumental|demo|"
    r"radio\s+(?:edit|version)|remaster(?:ed)?(?:\s+\d{4})?|sped[ -]?up|"
    r"slowed(?:\s*[+&]\s*reverb)?|nightcore))$",
    re.IGNORECASE,
)
_INLINE_CONTEXT = (
    re.compile(r"\blive\s+(?:at|from|version|recording|performance|session)\b[^\r\n]{0,80}", re.I),
    re.compile(
        r"\b(?:acoustic|cover|karaoke|instrumental|demo)\s+"
        r"(?:version|recording|performance|session)\b",
        re.I,
    ),
    re.compile(r"\bradio\s+(?:edit|version)\b", re.I),
    re.compile(r"\bremaster(?:ed)?(?:\s+\d{4})?\b", re.I),
    re.compile(r"\bsped[ -]?up\b", re.I),
    re.compile(r"\bslowed(?:\s*[+&]\s*reverb)?\b", re.I),
    re.compile(r"\bnightcore\b", re.I),
)


def canonical_recording_version_labels(*values: str | None) -> tuple[str, ...]:
    """Classify aliases into the one durable recording-version vocabulary."""

    combined = " ".join(
        unicodedata.normalize("NFKC", value).replace("_", " ") for value in values if value
    )
    return tuple(
        label for label, pattern in CANONICAL_RECORDING_VERSION_PATTERNS if pattern.search(combined)
    )


def recording_version_evidence(*values: str | None) -> tuple[str, ...]:
    """Return only title fragments that actually look like performance qualifiers.

    A context-free word search mistakes titles such as ``Live Forever`` and artist-title
    strings such as ``Live - Lightning Crashes`` for live recordings. Qualifiers are
    accepted when they are standalone structured values, bracketed/suffixed annotations,
    or explicit recording phrases such as ``live at Wembley``.
    """

    result: list[str] = []
    for raw in values:
        if not raw:
            continue
        value = unicodedata.normalize("NFKC", raw).strip()
        if not value:
            continue
        if _WHOLE_VERSION.fullmatch(value):
            result.append(value)
        for match in _BRACKETED.finditer(value):
            fragment = match.group("value").strip()
            if _SUFFIX_VERSION.fullmatch(fragment) or any(
                pattern.search(fragment) for pattern in _INLINE_CONTEXT
            ):
                result.append(fragment)
        trailing = _TRAILING_MARKER.search(value)
        if trailing is not None:
            result.append(trailing.group("value"))
        suffixes = list(_SEPARATOR_SUFFIX.finditer(value))
        if suffixes:
            suffix = suffixes[-1].group("value").strip()
            if _SUFFIX_VERSION.fullmatch(suffix):
                result.append(suffix)
        for pattern in _INLINE_CONTEXT:
            result.extend(match.group(0) for match in pattern.finditer(value))
    return tuple(dict.fromkeys(result))
