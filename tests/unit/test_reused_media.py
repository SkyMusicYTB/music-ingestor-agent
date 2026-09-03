from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.services.filesystem import sha256_file
from app.tags import EmbeddedArtwork, MediaTags, write_tags
from app.workers.media import MediaProcessor, MediaValidationError, parse_probe_payload


def _payload(video):
    return {
        "format": {"format_name": "mp3", "duration": "0.25"},
        "streams": [{"codec_type": "audio", "codec_name": "mp3"}, video],
    }


@pytest.mark.parametrize("codec", ["mjpeg", "png"])
def test_attached_art_requires_explicit_retained_audio_opt_in(tmp_path, codec):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"probe fixture")
    payload = _payload(
        {"codec_type": "video", "codec_name": codec, "disposition": {"attached_pic": 1}}
    )
    with pytest.raises(MediaValidationError, match="video stream"):
        parse_probe_payload(path, payload, max_duration_seconds=1800)
    accepted = parse_probe_payload(
        path, payload, max_duration_seconds=1800, allow_attached_art=True
    )
    assert accepted.codec == "mp3"


@pytest.mark.parametrize(
    "video",
    [
        {"codec_type": "video", "codec_name": "h264", "disposition": {"attached_pic": 1}},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 0}},
        {"codec_type": "video", "codec_name": "png", "disposition": {"attached_pic": "1"}},
        {"codec_type": "video", "codec_name": "png", "disposition": []},
        {"codec_type": "video", "codec_name": "png"},
    ],
)
def test_retained_audio_still_rejects_real_video_and_malformed_art(video):
    with pytest.raises(MediaValidationError, match="video stream"):
        parse_probe_payload(
            Path("audio.mp3"), _payload(video), max_duration_seconds=1800, allow_attached_art=True
        )


@pytest.mark.parametrize(("extension", "codec"), [("mp3", "libmp3lame"), ("m4a", "aac")])
def test_synthetic_tagged_artwork_survives_retained_audio_verification(tmp_path, extension, codec):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        if os.environ.get("CI") or os.environ.get("MUSIC_AGENT_REQUIRE_MEDIA_FIXTURES") == "1":
            pytest.fail("ffmpeg and ffprobe are required for retained-audio contract tests")
        pytest.skip("ffmpeg and ffprobe are required for synthetic media")
    path = tmp_path / f"retained.{extension}"
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
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
            codec,
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    cover = io.BytesIO()
    Image.new("RGB", (16, 16), "purple").save(cover, format="PNG")
    write_tags(
        path,
        MediaTags(title="Synthetic", artists=("Test Artist",)),
        EmbeddedArtwork(cover.getvalue(), "image/png", 16, 16),
    )
    digest = sha256_file(path)
    processor = MediaProcessor(ffmpeg=ffmpeg, ffprobe=ffprobe)
    with pytest.raises(MediaValidationError, match="video stream"):
        processor.normalize_and_verify(path, max_duration_seconds=1800, allow_lossy_transcode=False)
    probe = processor.normalize_and_verify(
        path,
        max_duration_seconds=1800,
        allow_lossy_transcode=False,
        allow_attached_art=True,
    )
    assert probe.path == path
    assert probe.codec == ("mp3" if extension == "mp3" else "aac")
    assert sha256_file(path) == digest
