from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import TXXX
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from PIL import Image

from app.tags import EmbeddedArtwork, MediaTags, read_tags, write_tags

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is required for synthetic media")


_FORMATS = [
    ("mp3", ["-c:a", "libmp3lame"]),
    ("m4a", ["-c:a", "aac"]),
    ("flac", ["-c:a", "flac"]),
    ("opus", ["-c:a", "libopus"]),
]


@pytest.mark.parametrize(("extension", "codec_args"), _FORMATS)
def test_format_specific_tags_and_artwork_round_trip(
    tmp_path: Path, extension: str, codec_args: list[str]
) -> None:
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
