from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any

from app.tags.models import MediaTags
from app.tags.provenance import (
    MUSICBRAINZ_ID_TAG_NAMES,
    PROVENANCE_TAG_FIELDS,
    provenance_snapshot,
    provenance_tag_text,
)

VORBIS_FIELDS = (
    "title",
    "artist",
    "album",
    "albumartist",
    "genre",
    "date",
    "tracknumber",
    "tracktotal",
    "discnumber",
    "disctotal",
    "musicbrainz_trackid",
    "musicbrainz_recordingid",
    "musicbrainz_albumid",
    "musicbrainz_releasegroupid",
    "music_agent_source_extractor",
    "music_agent_source_id",
    "music_agent_source_url",
    "music_agent_job_id",
    *(key.lower() for key in PROVENANCE_TAG_FIELDS),
)


def write_vorbis_fields(container: MutableMapping[str, Any], tags: MediaTags) -> None:
    for key in list(container.keys()):
        if key.casefold() in MUSICBRAINZ_ID_TAG_NAMES:
            del container[key]
    values: dict[str, tuple[str, ...]] = {
        "title": (tags.title,),
        "artist": tags.artists,
        "album": (tags.album,) if tags.album else (),
        "albumartist": tags.album_artists,
        "genre": tags.genres,
        "date": (tags.date,) if tags.date else (),
        "tracknumber": (str(tags.track_number),) if tags.track_number else (),
        "tracktotal": (str(tags.track_total),) if tags.track_total else (),
        "discnumber": (str(tags.disc_number),) if tags.disc_number else (),
        "disctotal": (str(tags.disc_total),) if tags.disc_total else (),
        "musicbrainz_trackid": (tags.recording_mbid,) if tags.recording_mbid else (),
        "musicbrainz_recordingid": (),
        "musicbrainz_albumid": (tags.release_mbid,) if tags.release_mbid else (),
        "musicbrainz_releasegroupid": (
            (tags.release_group_mbid,) if tags.release_group_mbid else ()
        ),
        "music_agent_source_extractor": ((tags.source_extractor,) if tags.source_extractor else ()),
        "music_agent_source_id": (tags.source_id,) if tags.source_id else (),
        "music_agent_source_url": (tags.source_url,) if tags.source_url else (),
        "music_agent_job_id": (tags.job_id,) if tags.job_id else (),
    }
    for key, attribute in PROVENANCE_TAG_FIELDS.items():
        value = provenance_tag_text(tags, attribute)
        values[key.lower()] = (value,) if value is not None else ()
    for key in VORBIS_FIELDS:
        current = values[key]
        if current:
            container[key] = list(current)
        elif key in container:
            del container[key]


def read_vorbis_snapshot(
    container: MutableMapping[str, Any], *, has_artwork: bool
) -> dict[str, Any]:
    return {
        "title": first_text(container.get("title")),
        "artists": texts(container.get("artist")),
        "album": first_text(container.get("album")),
        "album_artists": texts(container.get("albumartist")),
        "genres": texts(container.get("genre")),
        "date": first_text(container.get("date")),
        "track_number": first_number(container.get("tracknumber")),
        "track_total": first_number(container.get("tracktotal")),
        "disc_number": first_number(container.get("discnumber")),
        "disc_total": first_number(container.get("disctotal")),
        "recording_mbid": first_text(container.get("musicbrainz_trackid")),
        "release_mbid": first_text(container.get("musicbrainz_albumid")),
        "release_group_mbid": first_text(container.get("musicbrainz_releasegroupid")),
        "source_extractor": first_text(container.get("music_agent_source_extractor")),
        "source_id": first_text(container.get("music_agent_source_id")),
        "source_url": first_text(container.get("music_agent_source_url")),
        "job_id": first_text(container.get("music_agent_job_id")),
        **provenance_snapshot(
            {
                attribute: first_text(container.get(key.lower()))
                for key, attribute in PROVENANCE_TAG_FIELDS.items()
            }
        ),
        "has_artwork": has_artwork,
    }


def texts(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item))
    return ()


def first_text(value: object) -> str | None:
    values = texts(value)
    return values[0] if values else None


def first_number(value: object) -> int | None:
    text = first_text(value)
    if text is None:
        return None
    head = text.split("/", 1)[0].strip()
    try:
        number = int(head)
    except ValueError:
        return None
    return number if number > 0 else None


def number_pair(value: object) -> tuple[int | None, int | None]:
    text = first_text(value)
    if text is None:
        return None, None
    parts = text.split("/", 1)
    number = _parse_positive(parts[0])
    total = _parse_positive(parts[1]) if len(parts) > 1 else None
    return number, total


def _parse_positive(value: str) -> int | None:
    try:
        result = int(value.strip())
    except ValueError:
        return None
    return result if result > 0 else None
