from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TXXX, UFID
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from PIL import Image

from app.services.library_metadata import read_audio_metadata
from app.services.library_scan import LibraryScanner
from app.tags import EmbeddedArtwork, MediaTags, read_tags, write_tags

FFMPEG = shutil.which("ffmpeg")


_FORMATS = [
    ("mp3", ["-c:a", "libmp3lame"]),
    ("m4a", ["-c:a", "aac"]),
    ("flac", ["-c:a", "flac"]),
    ("opus", ["-c:a", "libopus"]),
]


@pytest.mark.parametrize(("extension", "codec_args"), _FORMATS)
def test_format_specific_tags_and_artwork_round_trip(
    tmp_path: Path, extension: str, codec_args: list[str], session_factory
) -> None:
    if FFMPEG is None:
        if os.environ.get("CI") or os.environ.get("MUSIC_AGENT_REQUIRE_MEDIA_FIXTURES") == "1":
            pytest.fail("ffmpeg is mandatory for synthetic tag/provenance tests")
        pytest.skip("ffmpeg is required for synthetic media")
    assert FFMPEG is not None
    media = tmp_path / f"synthetic.{extension}"
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "0.25",
        *codec_args,
        str(media),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        if os.environ.get("CI") or os.environ.get("MUSIC_AGENT_REQUIRE_MEDIA_FIXTURES") == "1":
            pytest.fail(f"ffmpeg lacks the required {extension} encoder")
        pytest.skip(f"ffmpeg lacks the {extension} encoder: {exc.stderr.decode(errors='replace')}")

    _add_unknown_tag(media, extension)
    artwork_buffer = io.BytesIO()
    Image.new("RGB", (24, 24), "purple").save(artwork_buffer, format="PNG")
    tags = MediaTags(
        title="Synthetic Song",
        artists=("Primary Artist", "Featured Artist"),
        album="Synthetic Album",
        album_artists=("Album Artist",),
        genres=("Test", "Electronic"),
        date="2026",
        track_number=2,
        track_total=9,
        disc_number=1,
        disc_total=2,
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
        release_group_mbid="33333333-3333-3333-3333-333333333333",
        source_extractor="youtube",
        source_id="dQw4w9WgXcQ",
        job_id="018f95dd-54ea-7c81-b60f-66626c956f9b",
    )
    write_tags(
        media,
        tags,
        EmbeddedArtwork(data=artwork_buffer.getvalue(), mime_type="image/png", width=24, height=24),
    )
    snapshot = read_tags(media)
    assert snapshot["title"] == "Synthetic Song"
    assert snapshot["artists"] == ("Primary Artist", "Featured Artist")
    assert snapshot["album_artists"] == ("Album Artist",)
    assert snapshot["track_number"] == 2
    assert snapshot["disc_number"] == 1
    assert snapshot["job_id"] == "018f95dd-54ea-7c81-b60f-66626c956f9b"
    assert snapshot["has_artwork"] is True
    _assert_unknown_tag(media, extension)
    if extension == "mp3":
        assert MP3(media).tags.version == (2, 4, 0)
        audio = MP3(media)
        audio.tags.add(UFID(owner="http://musicbrainz.org", data=tags.recording_mbid.encode()))
        audio.tags.add(TXXX(encoding=3, desc="musicbrainz_trackid", text=[tags.recording_mbid]))
        audio.save()
    elif extension == "m4a":
        audio = MP4(media)
        audio["----:com.apple.iTunes:musicbrainz_recordingid"] = [tags.recording_mbid.encode()]
        audio.save()
    else:
        audio = FLAC(media) if extension == "flac" else OggOpus(media)
        audio["MusicBrainz Track Id"] = [tags.recording_mbid]
        audio.save()

    # A provider fallback clears every prior MusicBrainz tag, including UFID,
    # but preserves artwork, the structured credit and bounded source provenance.
    fallback = MediaTags(
        title="Tarantella",
        artists=("Gabry Ponte", "KEL"),
        source_provider="youtube",
        source_extractor="youtube",
        source_id="rxw1RCAY3qw",
        source_url="https://www.youtube.com/watch?v=rxw1RCAY3qw",
        source_uploader="Gabry Ponte",
        metadata_authority="direct_user_source",
        canonical_identity_verified=False,
        metadata_provenance={
            "canonical_metadata_resolution": {
                "source": "direct_user_source",
                "automatic_association": True,
                "reason_code": "no_candidates",
                "decided_by": "deterministic",
            }
        },
        recording_mbid=tags.recording_mbid,
        release_mbid=tags.release_mbid,
        release_group_mbid=tags.release_group_mbid,
        job_id=tags.job_id,
    )
    snapshot = write_tags(media, fallback)
    assert snapshot["has_artwork"] is True
    assert snapshot["recording_mbid"] is None
    assert snapshot["release_mbid"] is None
    assert snapshot["release_group_mbid"] is None
    assert snapshot["source_uploader"] == "Gabry Ponte"
    assert snapshot["canonical_identity_verified"] is False
    assert snapshot["metadata_authority"] == "direct_user_source"
    resolution = snapshot["metadata_provenance"]["canonical_metadata_resolution"]
    assert resolution["reason_code"] == "no_candidates"
    assert resolution["decided_by"] == "deterministic"
    parsed = read_audio_metadata(media, music_root=tmp_path)
    assert parsed["artist"] == "Gabry Ponte, KEL"
    assert parsed["recording_mbid"] is None
    assert parsed["canonical_identity_verified"] is False
    scanner = LibraryScanner(session_factory, tmp_path)
    track = scanner.index_one(media)
    assert track.recording_mbid is None
    assert track.release_mbid is None
    assert track.release_group_mbid is None
    assert track.source_id == "rxw1RCAY3qw"
    assert track.artist == "Gabry Ponte, KEL"
    provenance = json.loads(track.provenance_json)
    assert provenance["canonical_identity_verified"] is False
    assert provenance["metadata_authority"] == "direct_user_source"
    assert provenance["source_provider"] == "youtube"
    assert provenance["source_uploader"] == "Gabry Ponte"
    assert provenance["artists"] == ["Gabry Ponte", "KEL"]
    assert provenance["metadata_provenance"]["canonical_metadata_resolution"] == resolution
    assert scanner.run(full=True).error_count == 0
    rescanned = scanner.index_one(media)
    assert json.loads(rescanned.provenance_json) == provenance
    _assert_unknown_tag(media, extension)


def _add_unknown_tag(path: Path, extension: str) -> None:
    if extension == "mp3":
        audio = MP3(path)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TXXX(encoding=3, desc="KEEP", text=["preserved"]))
    elif extension == "m4a":
        audio = MP4(path)
        audio["\xa9cmt"] = ["preserved"]
    elif extension == "flac":
        audio = FLAC(path)
        audio["custom_keep"] = ["preserved"]
    else:
        audio = OggOpus(path)
        audio["custom_keep"] = ["preserved"]
    audio.save()


def _assert_unknown_tag(path: Path, extension: str) -> None:
    if extension == "mp3":
        assert MP3(path).tags.getall("TXXX:KEEP")[0].text == ["preserved"]
    elif extension == "m4a":
        assert MP4(path)["\xa9cmt"] == ["preserved"]
    elif extension == "flac":
        assert FLAC(path)["custom_keep"] == ["preserved"]
    else:
        assert OggOpus(path)["custom_keep"] == ["preserved"]
