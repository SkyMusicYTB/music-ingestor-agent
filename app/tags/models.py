from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from app.tags.provenance import (
    PROVIDER_AUTHORITIES,
    bounded_text,
    canonical_verified,
    metadata_authority,
    sanitized_provenance,
)


@dataclass(frozen=True, slots=True)
class EmbeddedArtwork:
    data: bytes
    mime_type: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("embedded artwork cannot be empty")
        if self.mime_type not in {"image/jpeg", "image/png"}:
            raise ValueError("embedded artwork must be JPEG or PNG")


@dataclass(frozen=True, slots=True)
class MediaTags:
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    album_artists: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    date: str | None = None
    track_number: int | None = None
    track_total: int | None = None
    disc_number: int | None = None
    disc_total: int | None = None
    recording_mbid: str | None = None
    release_mbid: str | None = None
    release_group_mbid: str | None = None
    source_extractor: str | None = None
    source_id: str | None = None
    source_url: str | None = None
    job_id: str | None = None
    source_provider: str | None = None
    source_uploader: str | None = None
    canonical_identity_verified: bool | None = None
    metadata_authority: str | None = None
    metadata_provenance: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if (
            not self.title.strip()
            or not self.artists
            or not all(item.strip() for item in self.artists)
        ):
            raise ValueError("title and at least one non-empty artist are required")
        for number in (self.track_number, self.track_total, self.disc_number, self.disc_total):
            if number is not None and number <= 0:
                raise ValueError("track and disc numbers must be positive")
        object.__setattr__(self, "source_provider", bounded_text(self.source_provider, 40))
        object.__setattr__(self, "source_uploader", bounded_text(self.source_uploader, 300))
        object.__setattr__(self, "metadata_authority", metadata_authority(self.metadata_authority))
        object.__setattr__(
            self, "metadata_provenance", sanitized_provenance(self.metadata_provenance)
        )
        verified = canonical_verified(self.canonical_identity_verified)
        if self.metadata_authority in PROVIDER_AUTHORITIES:
            verified = False
        object.__setattr__(self, "canonical_identity_verified", verified)
        if verified is False:
            for field in ("recording_mbid", "release_mbid", "release_group_mbid"):
                object.__setattr__(self, field, None)


TrackTags = MediaTags


def coerce_media_tags(value: MediaTags | Mapping[str, Any]) -> MediaTags:
    if isinstance(value, MediaTags):
        return value
    title = value.get("title")
    if not isinstance(title, str):
        raise ValueError("tag metadata requires a title")
    artists = _strings(value.get("artists"))
    if not artists:
        artists = _strings(value.get("artist"))
    album_artists = _strings(value.get("album_artists"))
    if not album_artists:
        album_artists = _strings(value.get("album_artist"))
    genres = _strings(value.get("genres"))
    if not genres:
        genres = _strings(value.get("genre"))
    date = _optional_string(value.get("date"))
    if date is None and value.get("year") is not None:
        date = str(value["year"])
    return MediaTags(
        title=title.strip(),
        artists=artists,
        album=_optional_string(value.get("album")),
        album_artists=album_artists,
        genres=genres,
        date=date,
        track_number=_positive_int(value.get("track_number")),
        track_total=_positive_int(value.get("track_total")),
        disc_number=_positive_int(value.get("disc_number")),
        disc_total=_positive_int(value.get("disc_total")),
        recording_mbid=_optional_string(value.get("recording_mbid")),
        release_mbid=_optional_string(value.get("release_mbid")),
        release_group_mbid=_optional_string(value.get("release_group_mbid")),
        source_extractor=_optional_string(value.get("source_extractor")),
        source_id=_optional_string(value.get("source_id")),
        source_url=_optional_string(value.get("source_url")),
        job_id=_optional_string(value.get("job_id")),
        source_provider=_optional_string(value.get("source_provider")),
        source_uploader=_optional_string(value.get("source_uploader")),
        canonical_identity_verified=canonical_verified(value.get("canonical_identity_verified")),
        metadata_authority=metadata_authority(value.get("metadata_authority")),
        metadata_provenance=sanitized_provenance(value.get("metadata_provenance")),
    )


def coerce_artwork(value: object | None) -> EmbeddedArtwork | None:
    if value is None:
        return None
    if isinstance(value, EmbeddedArtwork):
        return value
    candidate = cast(Any, value)
    try:
        data = candidate.data
        mime_type = candidate.mime_type
    except AttributeError as exc:
        raise ValueError("artwork must provide data and mime_type") from exc
    return EmbeddedArtwork(
        data=bytes(data),
        mime_type=str(mime_type),
        width=_positive_int(getattr(candidate, "width", None)),
        height=_positive_int(getattr(candidate, "height", None)),
    )


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items: Sequence[object] = [value]
    elif isinstance(value, Sequence):
        items = value
    else:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item.strip():
            continue
        cleaned = item.strip()
        if cleaned.casefold() not in seen:
            result.append(cleaned)
            seen.add(cleaned.casefold())
    return tuple(result)


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None
