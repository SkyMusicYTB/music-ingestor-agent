from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.services.duplicates import version_signature
from app.services.recording_versions import recording_version_evidence
from app.sources import ProviderIdentity, provider_for_url

_ACQUISITION_PROVIDERS = (
    ProviderIdentity.BANDCAMP,
    ProviderIdentity.SOUNDCLOUD,
    ProviderIdentity.YOUTUBE,
)
_PROVIDER_NAMES = "|".join(provider.value for provider in _ACQUISITION_PROVIDERS)
_PROVIDER_PATTERNS = (
    re.compile(
        rf"\b(?:from|via|using|through)\s+(?:the\s+)?(?P<provider>{_PROVIDER_NAMES})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bon\s+(?:the\s+)?(?P<provider>{_PROVIDER_NAMES})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?P<provider>{_PROVIDER_NAMES})\s+(?:only|exclusively)\b",
        re.IGNORECASE,
    ),
)
_NEGATED_PROVIDER_PREFIX = re.compile(r"\b(?:not|except|without)\s*$", re.IGNORECASE)
_NEGATED_PROVIDER_PATTERN = re.compile(
    rf"\b(?:not\s+(?:(?:from|on|via|using|through)\s+)?|except(?:\s+for)?\s+|without\s+)"
    rf"(?:the\s+)?(?P<provider>{_PROVIDER_NAMES})\b",
    re.IGNORECASE,
)
_ALBUM_QUOTED = re.compile(
    r"\b(?:from|on)\s+(?:the\s+)?(?:album|release)\s+"
    r"(?:\"(?P<double>[^\"]{1,300})\"|'(?P<single>[^']{1,300})'|"
    r"[\u201c\u2018](?P<curly>[^\u201d\u2019]{1,300})[\u201d\u2019])",
    re.IGNORECASE,
)
_ALBUM_BARE = re.compile(
    rf"\b(?:from|on)\s+(?:the\s+)?(?:album|release)\s+"
    rf"(?P<album>[^,;.!?]{{1,300}}?)"
    rf"(?=\s+(?:by\b|(?:via|using|through|from|on)\s+(?:the\s+)?(?:{_PROVIDER_NAMES})\b)|$)",
    re.IGNORECASE,
)
_ALBUM_LABEL = re.compile(
    r"\balbum\s*:\s*(?:\"(?P<double>[^\"]{1,300})\"|'(?P<single>[^']{1,300})'|"
    r"(?P<bare>[^,;.!?]{1,300}?)(?=\s+by\b|$))",
    re.IGNORECASE,
)
_EXPLICIT_VERSION = re.compile(
    r"\b(?P<value>live|acoustic|remix|remixed|demo|instrumental|karaoke|cover|"
    r"remaster(?:ed)?(?:\s+\d{4})?|radio\s+edit|sped[ -]?up|slowed(?:\s*[+&]\s*reverb)?|"
    r"extended(?:\s+mix)?)\s+(?:version|recording)\b",
    re.IGNORECASE,
)
_STRONG_VERSION = re.compile(
    r"\b(?P<value>radio\s+edit|sped[ -]?up|slowed(?:\s*[+&]\s*reverb)?|extended\s+mix|"
    r"remaster(?:ed)?\s+\d{4})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ExplicitRequestConstraints:
    """Only constraints that can be attributed directly to the user's input."""

    provider: str | None = None
    providers: tuple[str, ...] = ()
    excluded_providers: tuple[str, ...] = ()
    album: str | None = None
    version: str | None = None

    def as_provenance(self) -> dict[str, object]:
        providers = self.providers or ((self.provider,) if self.provider is not None else ())
        return {
            "requested_provider": self.provider,
            "requested_providers": list(providers),
            "excluded_providers": list(self.excluded_providers),
            "provider_constraint_explicit": bool(providers or self.excluded_providers),
            "requested_album": self.album,
            "album_constraint_explicit": self.album is not None,
            "requested_version": self.version,
            "version_constraint_explicit": self.version is not None,
        }


def parse_explicit_request_constraints(
    text: str, *, input_kind: str = "natural_language"
) -> ExplicitRequestConstraints:
    normalized = unicodedata.normalize("NFKC", text).strip()
    if input_kind in {"youtube_url", "media_url"}:
        provider = provider_for_url(normalized)
        provider_name = provider.value if provider in _ACQUISITION_PROVIDERS else None
        return ExplicitRequestConstraints(
            provider=provider_name,
            providers=((provider_name,) if provider_name is not None else ()),
        )
    providers, excluded_providers = _explicit_providers(normalized)
    album, album_span = _explicit_album_with_span(normalized)
    return ExplicitRequestConstraints(
        provider=providers[0] if len(providers) == 1 else None,
        providers=providers,
        excluded_providers=excluded_providers,
        album=album,
        version=_explicit_version(_mask_span(normalized, album_span)),
    )


def _explicit_providers(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    excluded = {
        match.group("provider").casefold() for match in _NEGATED_PROVIDER_PATTERN.finditer(value)
    }
    providers: set[str] = set()
    for pattern in _PROVIDER_PATTERNS:
        for match in pattern.finditer(value):
            prefix = value[max(0, match.start() - 16) : match.start()]
            if _NEGATED_PROVIDER_PREFIX.search(prefix):
                excluded.add(match.group("provider").casefold())
                continue
            providers.add(match.group("provider").casefold())
    providers.difference_update(excluded)
    order = {provider.value: index for index, provider in enumerate(_ACQUISITION_PROVIDERS)}
    return (
        tuple(sorted(providers, key=order.__getitem__)),
        tuple(sorted(excluded, key=order.__getitem__)),
    )


def _explicit_album(value: str) -> str | None:
    album, _span = _explicit_album_with_span(value)
    return album


def _explicit_album_with_span(value: str) -> tuple[str | None, tuple[int, int] | None]:
    """Return release text and the complete phrase that supplied it.

    Album/release names are packaging context, not recording-version evidence.
    The source span lets callers ignore words such as ``Live`` inside the album
    phrase while retaining an independent ``live version`` qualifier elsewhere.
    """

    for pattern in (_ALBUM_QUOTED, _ALBUM_LABEL, _ALBUM_BARE):
        match = pattern.search(value)
        if match is None:
            continue
        raw = next(
            (
                group
                for name in ("double", "single", "curly", "bare", "album")
                if (group := match.groupdict().get(name)) is not None
            ),
            None,
        )
        if raw is None:
            continue
        cleaned = " ".join(raw.split()).strip(" \t\r\n\"'\u201c\u201d\u2018\u2019")
        if cleaned:
            return cleaned[:300], match.span()
    return None, None


def _mask_span(value: str, span: tuple[int, int] | None) -> str:
    if span is None:
        return value
    start, end = span
    return f"{value[:start]}{' ' * (end - start)}{value[end:]}"


def _explicit_version(value: str) -> str | None:
    samples = list(recording_version_evidence(value))
    samples.extend(match.group("value") for match in _EXPLICIT_VERSION.finditer(value))
    samples.extend(match.group("value") for match in _STRONG_VERSION.finditer(value))
    signatures = {
        signature for sample in samples if (signature := version_signature(sample)) != "studio"
    }
    if not signatures:
        return None
    parts = sorted({part for signature in signatures for part in signature.split("|")})
    return "|".join(parts)
