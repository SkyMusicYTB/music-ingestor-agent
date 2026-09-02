from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from urllib.parse import urlsplit

from app.sources.identities import ProviderIdentity, ProviderUse

MAX_PROVIDER_DESCRIPTION_LENGTH = 2_000
# Only evidence created by trusted local provider workflows may cross from display
# metadata into URL probing/acquisition. Model proposal evidence is intentionally absent.
EXECUTABLE_EVIDENCE_KINDS = frozenset(
    {"direct_user_url", "direct_collection_item", "provider_search_result"}
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    provider: ProviderIdentity
    acquisition: bool
    evidence: bool
    canonical_extractor: str
    extractor_aliases: tuple[str, ...]
    host_suffixes: tuple[str, ...]
    reliability: float = 1.0

    def supports(self, use: ProviderUse) -> bool:
        return self.acquisition if use is ProviderUse.ACQUISITION else self.evidence

    def accepts_hostname(self, hostname: str) -> bool:
        normalized = hostname.rstrip(".").casefold()
        return any(
            normalized == suffix or normalized.endswith(f".{suffix}")
            for suffix in self.host_suffixes
        )


_CAPABILITIES = {
    ProviderIdentity.YOUTUBE: ProviderCapability(
        provider=ProviderIdentity.YOUTUBE,
        acquisition=True,
        evidence=True,
        canonical_extractor="youtube",
        extractor_aliases=(
            "youtube",
            "youtube:search",
            "youtube:tab",
            "youtubetab",
            "youtubesearch",
        ),
        host_suffixes=("youtube.com", "youtu.be"),
    ),
    ProviderIdentity.SOUNDCLOUD: ProviderCapability(
        provider=ProviderIdentity.SOUNDCLOUD,
        acquisition=True,
        evidence=True,
        canonical_extractor="soundcloud",
        extractor_aliases=(
            "soundcloud",
            "soundcloud:search",
            "soundcloud:set",
            "soundcloud:user",
            "soundcloudsearch",
        ),
        host_suffixes=("soundcloud.com",),
    ),
    ProviderIdentity.BANDCAMP: ProviderCapability(
        provider=ProviderIdentity.BANDCAMP,
        acquisition=True,
        evidence=True,
        canonical_extractor="bandcamp",
        extractor_aliases=("bandcamp", "bandcamp:album", "bandcamp:track"),
        host_suffixes=("bandcamp.com",),
    ),
    ProviderIdentity.SPOTIFY: ProviderCapability(
        provider=ProviderIdentity.SPOTIFY,
        acquisition=False,
        evidence=True,
        canonical_extractor="spotify",
        extractor_aliases=("spotify",),
        host_suffixes=("spotify.com",),
    ),
    ProviderIdentity.APPLE: ProviderCapability(
        provider=ProviderIdentity.APPLE,
        acquisition=False,
        evidence=True,
        canonical_extractor="apple",
        extractor_aliases=("apple", "applemusic", "itunes"),
        host_suffixes=("music.apple.com",),
    ),
    ProviderIdentity.MUSICBRAINZ: ProviderCapability(
        provider=ProviderIdentity.MUSICBRAINZ,
        acquisition=False,
        evidence=True,
        canonical_extractor="musicbrainz",
        extractor_aliases=("musicbrainz",),
        host_suffixes=("musicbrainz.org",),
    ),
}

PROVIDER_CAPABILITIES = MappingProxyType(_CAPABILITIES)
_EXTRACTOR_PROVIDERS = MappingProxyType(
    {
        alias: capability.provider
        for capability in _CAPABILITIES.values()
        for alias in capability.extractor_aliases
    }
)


def provider_capability(provider: ProviderIdentity | str) -> ProviderCapability:
    return PROVIDER_CAPABILITIES[ProviderIdentity(provider)]


def provider_for_extractor(extractor: str) -> ProviderIdentity | None:
    return _EXTRACTOR_PROVIDERS.get(extractor.strip().casefold())


def provider_for_url(url: str) -> ProviderIdentity | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    if hostname is None:
        return None
    matches = [
        capability.provider
        for capability in _CAPABILITIES.values()
        if capability.accepts_hostname(hostname)
    ]
    return matches[0] if len(matches) == 1 else None


def bound_provider_description(
    value: object,
    *,
    limit: int = MAX_PROVIDER_DESCRIPTION_LENGTH,
) -> str | None:
    """Normalize and bound provider prose; callers must still treat it as untrusted data."""

    if not isinstance(value, str) or limit <= 0:
        return None
    normalized = unicodedata.normalize("NFKC", _CONTROL_RE.sub("", value)).strip()
    return normalized[:limit] or None
