from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from app.tags.models import EmbeddedArtwork, MediaTags, coerce_artwork, coerce_media_tags


class TaggingError(RuntimeError):
    pass


class UnsupportedMediaFormat(TaggingError):
    pass


class TagAdapter(Protocol):
    def write(self, path: Path, tags: MediaTags, artwork: EmbeddedArtwork | None) -> None: ...

    def read(self, path: Path) -> dict[str, Any]: ...


def adapter_for_path(path: Path) -> TagAdapter:
    extension = path.suffix.casefold()
    if extension == ".mp3":
        from app.tags.mp3 import MP3TagAdapter

        return MP3TagAdapter()
    if extension in {".m4a", ".mp4"}:
        from app.tags.mp4 import MP4TagAdapter

        return MP4TagAdapter()
    if extension == ".flac":
        from app.tags.flac import FLACTagAdapter

        return FLACTagAdapter()
    if extension in {".opus", ".ogg", ".oga"}:
        from app.tags.oggopus import OggOpusTagAdapter

        return OggOpusTagAdapter()
    raise UnsupportedMediaFormat(f"unsupported media extension: {extension or '<none>'}")


def write_tags(
    path: Path,
    tags: MediaTags | Mapping[str, Any],
    artwork: object | None = None,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise TaggingError("tag target must be a regular non-symlink file")
    expected = coerce_media_tags(tags)
    embedded = coerce_artwork(artwork)
    adapter = adapter_for_path(path)
    try:
        adapter.write(path, expected, embedded)
        actual = adapter.read(path)
    except TaggingError:
        raise
    except Exception as exc:
        raise TaggingError(f"failed to tag {path.name}") from exc
    if verify:
        verify_tag_snapshot(expected, embedded, actual)
    return actual


def read_tags(path: Path) -> dict[str, Any]:
    try:
        return adapter_for_path(path).read(path)
    except TaggingError:
        raise
    except Exception as exc:
        raise TaggingError(f"failed to read tags from {path.name}") from exc


def verify_tag_snapshot(
    expected: MediaTags,
    artwork: EmbeddedArtwork | None,
    actual: Mapping[str, Any],
) -> None:
    if actual.get("title") != expected.title:
        raise TaggingError("tag readback did not preserve the title")
    actual_artists = tuple(actual.get("artists") or ())
    if actual_artists != expected.artists:
        raise TaggingError("tag readback did not preserve artist values")
    if expected.album is not None and actual.get("album") != expected.album:
        raise TaggingError("tag readback did not preserve the album")
    if (
        expected.album_artists
        and tuple(actual.get("album_artists") or ()) != expected.album_artists
    ):
        raise TaggingError("tag readback did not preserve album-artist values")
    if expected.track_number is not None and actual.get("track_number") != expected.track_number:
        raise TaggingError("tag readback did not preserve the track number")
    if expected.disc_number is not None and actual.get("disc_number") != expected.disc_number:
        raise TaggingError("tag readback did not preserve the disc number")
    if expected.job_id is not None and actual.get("job_id") != expected.job_id:
        raise TaggingError("tag readback did not preserve the job provenance")
    for attribute in (
        "recording_mbid",
        "release_mbid",
        "release_group_mbid",
        "source_provider",
        "source_uploader",
        "canonical_identity_verified",
        "metadata_authority",
        "metadata_provenance",
    ):
        if actual.get(attribute) != getattr(expected, attribute):
            raise TaggingError(f"tag readback did not preserve {attribute}")
    if artwork is not None and not actual.get("has_artwork"):
        raise TaggingError("tag readback did not find embedded artwork")
