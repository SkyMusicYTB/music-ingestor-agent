"""Offline generated audio covers every advertised container/codec combination."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image

from app.services.library_formats import FORMATS
from app.services.library_metadata import LibraryReadError, read_audio_metadata
from app.services.library_scan import LibraryScanner

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
ENCODERS = {"mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis"}
MUXERS = {
    ".m4a": "ipod",
    ".m4b": "ipod",
    ".mp4": "mp4",
    ".aif": "aiff",
    ".aiff": "aiff",
    ".aifc": "aiff",
    ".wma": "asf",
    ".asf": "asf",
    ".mka": "matroska",
    ".oga": "ogg",
    ".opus": "ogg",
    ".aac": "adts",
}
CASES = [
    (extension, codec) for extension, policy in FORMATS.items() for codec in sorted(policy.codecs)
]


def require_tools():
    if not FFMPEG or not FFPROBE:
        if os.environ.get("CI") or os.environ.get("MUSIC_AGENT_REQUIRE_MEDIA_FIXTURES") == "1":
            pytest.fail("ffmpeg and ffprobe are mandatory for library support contract tests")
        pytest.skip("ffmpeg and ffprobe are required for synthetic library media")


def generate(path: Path, codec: str, *, tags: bool = True):
    require_tools()
    args = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "0.25",
        "-c:a",
        ENCODERS.get(codec, codec),
    ]
    if tags:
        args += [
            "-metadata",
            "title=Synthetic Song",
            "-metadata",
            "artist=Synthetic Artist",
            "-metadata",
            "album=Synthetic Album",
        ]
    if path.suffix.casefold() in MUXERS:
        args += ["-f", MUXERS[path.suffix.casefold()]]
    args.append(str(path))
    subprocess.run(args, check=True, capture_output=True, timeout=15)  # noqa: S603


@pytest.mark.parametrize(("extension", "codec"), CASES)
def test_every_claimed_format_synthetic_roundtrip(tmp_path, extension, codec):
    path = tmp_path / f"synthetic{extension}"
    generate(path, codec)
    result = read_audio_metadata(path, music_root=tmp_path)
    assert result["codec"] == codec
    assert result["file_extension"] == extension
    assert 0 < result["duration_seconds"] < 2


def test_generated_mixed_library_scan_and_immediate_index(session_factory, settings):
    for extension, codec in [
        (".MP3", "mp3"),
        (".m4a", "aac"),
        (".flac", "flac"),
        (".ogg", "vorbis"),
        (".opus", "opus"),
        (".wav", "pcm_s16le"),
    ]:
        path = settings.music_path / f"synthetic{extension}"
        generate(path, codec)
    scanner = LibraryScanner(session_factory, settings.music_path)
    result = scanner.run()
    assert result.scanned_files == 6 and result.error_count == 0
    assert scanner.run().changed_files == 0
    path = settings.music_path / "untagged.wav"
    generate(path, "pcm_s16le", tags=False)
    assert scanner.index_one(path).title == "untagged"


def test_m4a_attached_artwork_is_not_rejected_as_video(tmp_path):
    path = tmp_path / "cover.m4a"
    generate(path, "aac")
    picture = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(picture, format="PNG")
    audio = MP4(path)
    audio["covr"] = [MP4Cover(picture.getvalue(), imageformat=MP4Cover.FORMAT_PNG)]
    audio.save()
    assert read_audio_metadata(path, music_root=tmp_path)["codec"] == "aac"


def test_opus_extension_does_not_claim_other_ogg_codecs(tmp_path):
    path = tmp_path / "mislabelled.opus"
    generate(path, "flac")
    with pytest.raises(LibraryReadError, match="unsupported_codec"):
        read_audio_metadata(path, music_root=tmp_path)


def test_disguised_playlist_is_never_a_library_audio_file(tmp_path):
    require_tools()
    path = tmp_path / "playlist.m4a"
    path.write_text("#EXTM3U\n#EXTINF:1,Fake\nhttp://127.0.0.1:9/private.mp3\n")
    with pytest.raises(LibraryReadError, match="malformed_audio"):
        read_audio_metadata(path, music_root=tmp_path)


@pytest.mark.parametrize("extension,muxer", [(".mp4", "mp4"), (".webm", "webm")])
def test_real_video_rejected(tmp_path, extension, muxer):
    require_tools()
    path = tmp_path / f"video{extension}"
    codec = "mpeg4" if muxer == "mp4" else "libvpx"
    subprocess.run(  # noqa: S603 - fixed synthetic fixture command
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=0.1",
            "-c:v",
            codec,
            "-f",
            muxer,
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    with pytest.raises(LibraryReadError, match="video_bearing"):
        read_audio_metadata(path, music_root=tmp_path)
