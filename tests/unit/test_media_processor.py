from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.clients.ytdlp import DownloadCancelled
from app.services.metadata_matching import MetadataCandidate, MetadataMatcher
from app.workers.media import MediaProbe, MediaProcessor
from app.workers.metadata import CanonicalMetadataResolution
from app.workers.processor import DownloadJobProcessor, JobNeedsReview


def _processor_for_metadata_check() -> DownloadJobProcessor:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.metadata_matcher = MetadataMatcher()
    return processor


def test_canonical_metadata_requires_auto_match() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=180,
        bitrate=160_000,
    )

    with pytest.raises(JobNeedsReview, match="does not confidently match"):
        processor._validate_canonical_metadata(
            {"artist": "Expected Artist", "title": "Expected Song"},
            {"artist": "Completely Different", "title": "Unrelated Upload"},
            probe,
        )


def test_canonical_metadata_allows_auto_match() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=180,
        bitrate=160_000,
    )

    processor._validate_canonical_metadata(
        {
            "artist": "Expected Artist",
            "title": "Expected Song",
            "duration_seconds": 180,
        },
        {"artist": "Expected Artist - Topic", "track": "Expected Song"},
        probe,
    )


class _CanonicalResolver:
    def __init__(self, resolution: CanonicalMetadataResolution) -> None:
        self.resolution = resolution

    def resolve(self, **_kwargs: object) -> CanonicalMetadataResolution:
        return self.resolution


def test_musicbrainz_auto_match_enriches_canonical_tags() -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    candidate = MetadataCandidate(
        artist="Canonical Artist",
        title="Canonical Song",
        album="Canonical Album",
        year=1999,
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
        release_group_mbid="33333333-3333-3333-3333-333333333333",
    )
    processor.metadata_resolver = _CanonicalResolver(  # type: ignore[assignment]
        CanonicalMetadataResolution("auto", candidate, (), "exact")
    )
    probe = MediaProbe(Path("download.opus"), "opus", ("ogg",), 180, 160_000)

    values = processor._resolve_canonical_metadata(
        {"artist": "Uploader", "title": "Upload Title", "version_signature": "studio"},
        probe,
    )

    assert values["artist"] == "Canonical Artist"
    assert values["title"] == "Canonical Song"
    assert values["album"] == "Canonical Album"
    assert values["recording_mbid"] == candidate.recording_mbid


def test_musicbrainz_review_match_stops_before_tagging() -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    candidate = MetadataCandidate(artist="Possible Artist", title="Possible Song")
    option = {
        "kind": "metadata",
        "rank": 1,
        "artist": candidate.artist,
        "title": candidate.title,
        "score": 0.82,
    }
    processor.metadata_resolver = _CanonicalResolver(  # type: ignore[assignment]
        CanonicalMetadataResolution("review", candidate, (option,), "ambiguous")
    )
    probe = MediaProbe(Path("download.opus"), "opus", ("ogg",), 180, 160_000)

    with pytest.raises(JobNeedsReview) as raised:
        processor._resolve_canonical_metadata(
            {"artist": "Artist", "title": "Song", "version_signature": "studio"},
            probe,
        )

    assert raised.value.options == [option]


def test_ffprobe_inspection_honors_cancellation_signal(tmp_path: Path) -> None:
    executable = tmp_path / "fake-media-tool"
    executable.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
    executable.chmod(0o755)
    media = tmp_path / "audio.opus"
    media.write_bytes(b"synthetic")
    processor = MediaProcessor(ffprobe=str(executable), ffmpeg=str(executable))
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(DownloadCancelled):
        processor.inspect(media, max_duration_seconds=1800, cancel_signal=cancelled)
