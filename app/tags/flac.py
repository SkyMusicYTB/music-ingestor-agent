from __future__ import annotations

# Mutagen 1.47's FLAC metadata-block unions do not model their mapping API.
# mypy: disable-error-code="no-untyped-call, var-annotated, arg-type"
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture

from app.tags.common import read_vorbis_snapshot, write_vorbis_fields
from app.tags.models import EmbeddedArtwork, MediaTags


class FLACTagAdapter:
    def write(self, path: Path, tags: MediaTags, artwork: EmbeddedArtwork | None) -> None:
        audio = FLAC(path)
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        write_vorbis_fields(audio.tags, tags)
        if artwork is not None:
            retained = [picture for picture in audio.pictures if picture.type != 3]
            audio.clear_pictures()
            for picture in retained:
                audio.add_picture(picture)
            audio.add_picture(_picture(artwork))
        audio.save()

    def read(self, path: Path) -> dict[str, Any]:
        audio = FLAC(path)
        tags = audio.tags or {}
        return read_vorbis_snapshot(tags, has_artwork=bool(audio.pictures))


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
