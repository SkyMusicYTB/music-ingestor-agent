from __future__ import annotations

# Mutagen 1.47's Ogg Opus classes are untyped.
# mypy: disable-error-code="no-untyped-call, var-annotated"
import base64
import binascii
from pathlib import Path
from typing import Any

from mutagen.flac import Picture
from mutagen.oggopus import OggOpus

from app.tags.common import read_vorbis_snapshot, write_vorbis_fields
from app.tags.models import EmbeddedArtwork, MediaTags


class OggOpusTagAdapter:
    def write(self, path: Path, tags: MediaTags, artwork: EmbeddedArtwork | None) -> None:
        audio = OggOpus(path)
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        write_vorbis_fields(audio.tags, tags)
        if artwork is not None:
            retained = _retained_pictures(audio.tags.get("metadata_block_picture", []))
            retained.append(_picture(artwork))
            audio.tags["metadata_block_picture"] = [
                base64.b64encode(picture.write()).decode("ascii") for picture in retained
            ]
            if "coverart" in audio.tags:
                del audio.tags["coverart"]
            if "coverartmime" in audio.tags:
                del audio.tags["coverartmime"]
        audio.save()

    def read(self, path: Path) -> dict[str, Any]:
        audio = OggOpus(path)
        tags = audio.tags or {}
        has_artwork = bool(tags.get("metadata_block_picture") or tags.get("coverart"))
        return read_vorbis_snapshot(tags, has_artwork=has_artwork)


def _retained_pictures(values: object) -> list[Picture]:
    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, list):
        candidates = values
    else:
        candidates = []
    retained: list[Picture] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        try:
            picture = Picture(base64.b64decode(value, validate=True))
        except (ValueError, binascii.Error):
            continue
        if picture.type != 3:
            retained.append(picture)
    return retained


def _picture(artwork: EmbeddedArtwork) -> Picture:
    picture = Picture()
    picture.type = 3
    picture.mime = artwork.mime_type
    picture.desc = "Cover"
    picture.width = artwork.width or 0
    picture.height = artwork.height or 0
    picture.depth = 24
    picture.data = artwork.data
    return picture
