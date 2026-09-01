from __future__ import annotations

from pathlib import Path

import pytest

from app.workers.media import MediaValidationError, parse_probe_payload


def test_probe_requires_one_audio_stream_no_video_and_bounded_duration(tmp_path: Path) -> None:
    path = tmp_path / "track.opus"
    path.write_bytes(b"placeholder")
    result = parse_probe_payload(
        path,
        {
            "streams": [{"codec_type": "audio", "codec_name": "opus"}],
            "format": {"format_name": "ogg", "duration": "180.5", "bit_rate": "160000"},
        },
        max_duration_seconds=600,
    )
    assert result.codec == "opus"
    assert result.container == ("ogg",)
    assert result.duration_seconds == 180.5

    with pytest.raises(MediaValidationError, match="video"):
        parse_probe_payload(
            path,
            {
                "streams": [
                    {"codec_type": "audio", "codec_name": "opus"},
                    {"codec_type": "video", "codec_name": "vp9"},
                ],
                "format": {"format_name": "webm", "duration": "180"},
            },
            max_duration_seconds=600,
        )


def test_probe_rejects_overlong_media(tmp_path: Path) -> None:
    path = tmp_path / "track.m4a"
    path.write_bytes(b"placeholder")
    with pytest.raises(MediaValidationError, match="duration"):
        parse_probe_payload(
            path,
            {
                "streams": [{"codec_type": "audio", "codec_name": "aac"}],
                "format": {"format_name": "mov,mp4,m4a", "duration": "601"},
            },
            max_duration_seconds=600,
        )
