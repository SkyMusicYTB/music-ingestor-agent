from __future__ import annotations

# Mutagen 1.47's MP4 classes are only partially typed.
# mypy: disable-error-code="no-untyped-call, var-annotated"
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4, MP4Cover

from app.tags.common import first_text, texts
from app.tags.models import EmbeddedArtwork, MediaTags

_FREEFORM_FIELDS = {
    "----:com.apple.iTunes:MusicBrainz Track Id": "recording_mbid",
    "----:com.apple.iTunes:MusicBrainz Album Id": "release_mbid",
    "----:com.apple.iTunes:MusicBrainz Release Group Id": "release_group_mbid",
    "----:com.apple.iTunes:MUSICBRAINZ_TRACKID": "recording_mbid",
    "----:com.apple.iTunes:MUSICBRAINZ_ALBUMID": "release_mbid",
    "----:com.apple.iTunes:MUSICBRAINZ_RELEASEGROUPID": "release_group_mbid",
    "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_EXTRACTOR": "source_extractor",
    "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_ID": "source_id",
    "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_URL": "source_url",
    "----:com.apple.iTunes:MUSIC_AGENT_JOB_ID": "job_id",
}


class MP4TagAdapter:
    def write(self, path: Path, tags: MediaTags, artwork: EmbeddedArtwork | None) -> None:
        audio = MP4(path)
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        values = audio.tags
        _set(values, "\xa9nam", [tags.title])
        _set(values, "\xa9ART", list(tags.artists))
        _set(values, "\xa9alb", [tags.album] if tags.album else None)
        _set(values, "aART", list(tags.album_artists) if tags.album_artists else None)
        _set(values, "\xa9gen", list(tags.genres) if tags.genres else None)
        _set(values, "\xa9day", [tags.date] if tags.date else None)
        _set(
            values,
            "trkn",
            [(tags.track_number, tags.track_total or 0)] if tags.track_number else None,
        )
        _set(
            values,
            "disk",
            [(tags.disc_number, tags.disc_total or 0)] if tags.disc_number else None,
        )
        for key, attribute in _FREEFORM_FIELDS.items():
            value = getattr(tags, attribute)
            _set(values, key, [value.encode("utf-8")] if value else None)
        if artwork is not None:
            image_format = (
                MP4Cover.FORMAT_JPEG if artwork.mime_type == "image/jpeg" else MP4Cover.FORMAT_PNG
            )
            values["covr"] = [MP4Cover(artwork.data, imageformat=image_format)]
        audio.save()

    def read(self, path: Path) -> dict[str, Any]:
        audio = MP4(path)
        values = audio.tags or {}
        track_number, track_total = _pair(values.get("trkn"))
        disc_number, disc_total = _pair(values.get("disk"))
        return {
            "title": first_text(values.get("\xa9nam")),
            "artists": texts(values.get("\xa9ART")),
            "album": first_text(values.get("\xa9alb")),
            "album_artists": texts(values.get("aART")),
            "genres": texts(values.get("\xa9gen")),
            "date": first_text(values.get("\xa9day")),
            "track_number": track_number,
            "track_total": track_total,
            "disc_number": disc_number,
            "disc_total": disc_total,
            "recording_mbid": _freeform_text(values, "----:com.apple.iTunes:MUSICBRAINZ_TRACKID"),
            "release_mbid": _freeform_text(values, "----:com.apple.iTunes:MUSICBRAINZ_ALBUMID"),
            "release_group_mbid": _freeform_text(
                values, "----:com.apple.iTunes:MUSICBRAINZ_RELEASEGROUPID"
            ),
            "source_extractor": _freeform_text(
                values, "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_EXTRACTOR"
            ),
            "source_id": _freeform_text(values, "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_ID"),
            "source_url": _freeform_text(values, "----:com.apple.iTunes:MUSIC_AGENT_SOURCE_URL"),
            "job_id": _freeform_text(values, "----:com.apple.iTunes:MUSIC_AGENT_JOB_ID"),
            "has_artwork": bool(values.get("covr")),
        }


def _set(values: Any, key: str, value: object | None) -> None:
    if value:
        values[key] = value
    elif key in values:
        del values[key]


def _pair(value: object) -> tuple[int | None, int | None]:
    if not isinstance(value, list) or not value:
        return None, None
    pair = value[0]
    if not isinstance(pair, tuple) or len(pair) < 2:
        return None, None
    number = int(pair[0]) if pair[0] else None
    total = int(pair[1]) if pair[1] else None
    return number, total


def _freeform_text(values: Any, key: str) -> str | None:
    items = values.get(key)
    if not isinstance(items, list) or not items:
        return None
    value = items[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value) if value is not None else None
