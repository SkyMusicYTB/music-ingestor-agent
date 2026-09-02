from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_EXTRACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,99}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class ProviderIdentity(StrEnum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"
    BANDCAMP = "bandcamp"
    SPOTIFY = "spotify"
    APPLE = "apple"
    MUSICBRAINZ = "musicbrainz"


class UploaderRelationship(StrEnum):
    OFFICIAL_ARTIST = "official_artist"
    OFFICIAL_LABEL = "official_label"
    TOPIC = "topic"
    DISTRIBUTOR = "distributor"
    THIRD_PARTY = "third_party"
    UNKNOWN = "unknown"


class ProviderUse(StrEnum):
    ACQUISITION = "acquisition"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True)
class ExtractorIdentity:
    """Stable provider identity, distinct from any transient finite candidate ID."""

    provider: ProviderIdentity
    extractor: str
    source_id: str

    def __post_init__(self) -> None:
        extractor = self.extractor.strip().casefold()
        source_id = self.source_id.strip()
        if not _EXTRACTOR_RE.fullmatch(extractor):
            raise ValueError("extractor must be a bounded safe identifier")
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError("source_id must be a bounded safe identifier")
        object.__setattr__(self, "extractor", extractor)
        object.__setattr__(self, "source_id", source_id)

    @property
    def stable_key(self) -> str:
        return f"{self.provider.value}:{self.extractor}:{self.source_id}"


def is_safe_candidate_id(value: str) -> bool:
    return bool(_SOURCE_ID_RE.fullmatch(value))
