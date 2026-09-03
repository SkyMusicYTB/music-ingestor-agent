from __future__ import annotations

# Mutagen 1.47 exposes incomplete typing for dynamically registered ID3 frames.
# mypy: disable-error-code="no-untyped-call, attr-defined"
from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TXXX,
    PictureType,
)
from mutagen.mp3 import MP3

from app.tags.common import number_pair
from app.tags.models import EmbeddedArtwork, MediaTags
from app.tags.provenance import (
    MUSICBRAINZ_ID_TAG_NAMES,
    PROVENANCE_TAG_FIELDS,
    provenance_snapshot,
    provenance_tag_text,
)

_TXXX_FIELDS = {
    "MusicBrainz Track Id": "recording_mbid",
    "MusicBrainz Album Id": "release_mbid",
    "MusicBrainz Release Group Id": "release_group_mbid",
    "MUSICBRAINZ_TRACKID": "recording_mbid",
    "MUSICBRAINZ_RECORDINGID": "recording_mbid",
    "MUSICBRAINZ_ALBUMID": "release_mbid",
    "MUSICBRAINZ_RELEASEGROUPID": "release_group_mbid",
    "MUSIC_AGENT_SOURCE_EXTRACTOR": "source_extractor",
    "MUSIC_AGENT_SOURCE_ID": "source_id",
    "MUSIC_AGENT_SOURCE_URL": "source_url",
    "MUSIC_AGENT_JOB_ID": "job_id",
    **PROVENANCE_TAG_FIELDS,
}


class MP3TagAdapter:
    def write(self, path: Path, tags: MediaTags, artwork: EmbeddedArtwork | None) -> None:
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        id3 = audio.tags
        for frame_id in ("TIT2", "TPE1", "TALB", "TPE2", "TCON", "TDRC", "TRCK", "TPOS"):
            id3.delall(frame_id)
        id3.add(TIT2(encoding=3, text=[tags.title]))
        id3.add(TPE1(encoding=3, text=list(tags.artists)))
        if tags.album:
            id3.add(TALB(encoding=3, text=[tags.album]))
        if tags.album_artists:
            id3.add(TPE2(encoding=3, text=list(tags.album_artists)))
        if tags.genres:
            id3.add(TCON(encoding=3, text=list(tags.genres)))
        if tags.date:
            id3.add(TDRC(encoding=3, text=[tags.date]))
        if tags.track_number:
            text = str(tags.track_number)
            if tags.track_total:
                text += f"/{tags.track_total}"
            id3.add(TRCK(encoding=3, text=[text]))
        if tags.disc_number:
            text = str(tags.disc_number)
            if tags.disc_total:
                text += f"/{tags.disc_total}"
            id3.add(TPOS(encoding=3, text=[text]))

        # Clear aliases and UFIDs as well as primary fields: null IDs in the
        # accepted snapshot must not resurrect stale MusicBrainz identity.
        for key, frame in list(id3.items()):
            if (
                isinstance(frame, TXXX) and frame.desc.casefold() in MUSICBRAINZ_ID_TAG_NAMES
            ) or getattr(frame, "owner", "") in {
                "http://musicbrainz.org",
                "https://musicbrainz.org",
            }:
                del id3[key]
        for description, attribute in _TXXX_FIELDS.items():
            id3.delall(f"TXXX:{description}")
            value = provenance_tag_text(tags, attribute)
            if value:
                id3.add(TXXX(encoding=3, desc=description, text=[value]))
        if artwork is not None:
            for key, frame in list(id3.items()):
                if isinstance(frame, APIC) and frame.type == PictureType.COVER_FRONT:
                    del id3[key]
            id3.add(
                APIC(
                    encoding=3,
                    mime=artwork.mime_type,
                    type=PictureType.COVER_FRONT,
                    desc="Cover",
                    data=artwork.data,
                )
            )
        audio.save(v2_version=4, v1=0)

    def read(self, path: Path) -> dict[str, Any]:
        audio = MP3(path)
        id3 = audio.tags
        if id3 is None:
            return {"title": None, "artists": (), "has_artwork": False}
        track_number, track_total = number_pair(_frame_text(id3.get("TRCK")))
        disc_number, disc_total = number_pair(_frame_text(id3.get("TPOS")))
        return {
            "title": _first_frame_text(id3.get("TIT2")),
            "artists": _frame_text(id3.get("TPE1")),
            "album": _first_frame_text(id3.get("TALB")),
            "album_artists": _frame_text(id3.get("TPE2")),
            "genres": _frame_text(id3.get("TCON")),
            "date": _first_frame_text(id3.get("TDRC")),
            "track_number": track_number,
            "track_total": track_total,
            "disc_number": disc_number,
            "disc_total": disc_total,
            "recording_mbid": _txxx_text(id3, "MUSICBRAINZ_TRACKID"),
            "release_mbid": _txxx_text(id3, "MUSICBRAINZ_ALBUMID"),
            "release_group_mbid": _txxx_text(id3, "MUSICBRAINZ_RELEASEGROUPID"),
            "source_extractor": _txxx_text(id3, "MUSIC_AGENT_SOURCE_EXTRACTOR"),
            "source_id": _txxx_text(id3, "MUSIC_AGENT_SOURCE_ID"),
            "source_url": _txxx_text(id3, "MUSIC_AGENT_SOURCE_URL"),
            "job_id": _txxx_text(id3, "MUSIC_AGENT_JOB_ID"),
            **provenance_snapshot(
                {
                    attribute: _txxx_text(id3, key)
                    for key, attribute in PROVENANCE_TAG_FIELDS.items()
                }
            ),
            "has_artwork": any(isinstance(frame, APIC) for frame in id3.values()),
        }


def _frame_text(frame: object) -> tuple[str, ...]:
    value = getattr(frame, "text", ())
    return tuple(str(item) for item in value)


def _first_frame_text(frame: object) -> str | None:
    values = _frame_text(frame)
    return values[0] if values else None


def _txxx_text(id3: Any, description: str) -> str | None:
    frames = id3.getall(f"TXXX:{description}")
    return _first_frame_text(frames[0]) if frames else None
