from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.models import ScanRun, Track
from app.repositories.library import LibraryRepository
from app.services import library_metadata, library_scan
from app.services.duplicates import DuplicateCandidate, DuplicateDetector
from app.services.library_audit import audit_library
from app.services.library_formats import FORMATS, PARSER_VERSION
from app.services.library_metadata import (
    LibraryReadError,
    filename_metadata,
    probe_metadata,
    read_audio_metadata,
)
from app.services.library_scan import LibraryScanner, ScanAlreadyRunning, ScanDiagnostics


def metadata(path: Path, *, music_root: Path | None = None) -> dict[str, object]:
    return {
        "artist": "Artist",
        "title": path.stem,
        "album": "Album",
        "duration_seconds": 1.0,
        "codec": "mp3",
        "source_id": None,
        "source_extractor": None,
        "version_signature": "studio",
    }


def create_file(root: Path, name: str = "song.mp3") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")
    return path


def test_library_recording_version_ignores_album_edition_text(monkeypatch, tmp_path: Path) -> None:
    class Info:
        length = 146.0
        bitrate = 192_000

    class MP3:
        info = Info()

        def __init__(self, tags):
            self.tags = tags

    path = create_file(tmp_path, "track.mp3")
    monkeypatch.setattr(
        library_metadata,
        "MutagenFile",
        lambda _file, *, easy: MP3(
            {
                "artist": ["Gabry Ponte & KEL"],
                "title": ["Tarantella"],
                "album": ["Radio Italia Live Compilation"],
            }
            if easy
            else {}
        ),
    )
    values = read_audio_metadata(path, music_root=tmp_path)
    assert values["version_signature"] == "studio"
    assert PARSER_VERSION == 3


def test_full_reread_and_parser_version_invalidation(monkeypatch, session_factory, settings):
    create_file(settings.music_path)
    calls = []

    def reader(path, **kwargs):
        calls.append(path)
        return metadata(path, **kwargs)

    monkeypatch.setattr(library_scan, "read_audio_metadata", reader)
    scanner = LibraryScanner(session_factory, settings.music_path)
    scanner.run()
    scanner.run()
    assert len(calls) == 1
    scanner.run(full=True)
    assert len(calls) == 2
    with session_factory.begin() as session:
        session.scalar(select(Track)).parser_version = 0
    scanner.run()
    assert len(calls) == 3
    assert LibraryRepository(session_factory).initial_scan_complete()


def test_initial_unreadable_scan_does_not_establish_baseline(
    monkeypatch, session_factory, settings
):
    create_file(settings.music_path)

    def unreadable(*args, **kwargs):
        raise PermissionError("secret absolute filename")

    monkeypatch.setattr(library_scan, "read_audio_metadata", unreadable)
    result = LibraryScanner(session_factory, settings.music_path).run()
    assert not LibraryRepository(session_factory).initial_scan_complete()
    summary = json.loads(result.summary_json)
    assert summary["counts"]["unreadable"] == 1
    assert "secret absolute" not in result.summary_json


def test_scan_lease_blocks_overlap_and_recovers_expired(monkeypatch, session_factory, settings):
    scanner = LibraryScanner(session_factory, settings.music_path)
    scan_id, generation, token = scanner._claim(False, None)
    with pytest.raises(ScanAlreadyRunning):
        scanner.run()
    with session_factory.begin() as session:
        session.get(ScanRun, scan_id).lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    result = scanner.run()
    assert result.generation == generation + 1
    with session_factory() as session:
        with pytest.raises(InterruptedError):
            scanner._fence(session, scan_id, token)


def test_simultaneous_scan_claims_have_one_winner(session_factory, settings):
    barrier = threading.Barrier(2)

    def claim():
        scanner = LibraryScanner(session_factory, settings.music_path)
        barrier.wait(timeout=5)
        try:
            return scanner._claim(False, None)[0]
        except ScanAlreadyRunning:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in claims) == 1


@pytest.mark.parametrize("immediate", [False, True])
def test_scanner_revalidates_path_before_indexing(
    monkeypatch, session_factory, settings, immediate
):
    path = create_file(settings.music_path)

    def reader(path, **kwargs):
        original = path.stat()
        result = {
            **metadata(path, **kwargs),
            "_file_device": original.st_dev,
            "_file_inode": original.st_ino,
            "_file_size": original.st_size,
            "_file_mtime_ns": original.st_mtime_ns,
        }
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(b"replacement")
        replacement.replace(path)
        return result

    monkeypatch.setattr(library_scan, "read_audio_metadata", reader)
    scanner = LibraryScanner(session_factory, settings.music_path)
    if immediate:
        with pytest.raises(LibraryReadError, match="file_changed"):
            scanner.index_one(path)
    else:
        result = scanner.run()
        assert result.changed_files == 0 and result.error_count == 1
        assert not LibraryRepository(session_factory).initial_scan_complete()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Track)) == 0


def test_concurrent_index_one_not_marked_missing(monkeypatch, session_factory, settings):
    first = create_file(settings.music_path)
    monkeypatch.setattr(library_scan, "read_audio_metadata", metadata)
    scanner = LibraryScanner(session_factory, settings.music_path)
    scanner.run()
    original = library_scan.iter_library_candidates

    def discovered(root, diagnostics, cancel_signal=None):
        yield from original(root, diagnostics, cancel_signal)
        scanner.index_one(create_file(root, "published.MP3"))

    monkeypatch.setattr(library_scan, "iter_library_candidates", discovered)
    scanner.run(full=True)
    with session_factory() as session:
        assert all(track.is_present for track in session.scalars(select(Track)))
        assert session.scalar(select(func.count()).select_from(Track)) == 2
    assert first.exists()


def test_unreadable_subtree_preserves_rows(monkeypatch, session_factory, settings):
    create_file(settings.music_path, "Artist/Album/song.mp3")
    monkeypatch.setattr(library_scan, "read_audio_metadata", metadata)
    scanner = LibraryScanner(session_factory, settings.music_path)
    scanner.run()

    def broken(root, diagnostics, cancel_signal=None):
        diagnostics.issue("Artist", "directory_unreadable")
        return iter(())

    monkeypatch.setattr(library_scan, "iter_library_candidates", broken)
    monkeypatch.setattr(library_scan, "library_presence", lambda *args: "unreadable")
    result = scanner.run(full=True)
    assert result.missing_files == 0 and result.traversal_complete is False
    with session_factory() as session:
        assert session.scalar(select(Track)).is_present
    assert LibraryRepository(session_factory).initial_scan_complete()


def test_source_alias_indexes_both_and_remains_duplicate_without_owner(
    monkeypatch, session_factory, settings
):
    def source_metadata(path, **kwargs):
        return {**metadata(path, **kwargs), "source_extractor": "youtube", "source_id": "source-id"}

    monkeypatch.setattr(library_scan, "read_audio_metadata", source_metadata)
    scanner = LibraryScanner(session_factory, settings.music_path)
    one = create_file(settings.music_path, "one.mp3")
    two = create_file(settings.music_path, "two.flac")
    owner = scanner.index_one(one)
    alias = scanner.index_one(two)
    assert owner.source_id == "source-id" and alias.source_id is None
    scanner.run(full=True)
    one.unlink()
    scanner.run()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Track)) == 2
        decision = DuplicateDetector(settings.music_path).find(
            session,
            DuplicateCandidate(
                artist="Other", title="Different", source_extractor="youtube", source_id="source-id"
            ),
        )
        assert decision.status == "owned" and decision.track_id == alias.id


def test_changed_file_does_not_keep_obsolete_source_alias(monkeypatch, session_factory, settings):
    monkeypatch.setattr(
        library_scan,
        "read_audio_metadata",
        lambda path, **kwargs: {
            **metadata(path, **kwargs),
            "source_extractor": "youtube",
            "source_id": "source-id",
        },
    )
    scanner = LibraryScanner(session_factory, settings.music_path)
    scanner.index_one(create_file(settings.music_path, "one.mp3"))
    path = create_file(settings.music_path, "two.mp3")
    assert "source_alias" in json.loads(scanner.index_one(path).provenance_json)
    monkeypatch.setattr(library_scan, "read_audio_metadata", metadata)
    assert "source_alias" not in json.loads(scanner.index_one(path).provenance_json)


def test_policy_shared_by_scan_and_index_one(monkeypatch, session_factory, settings):
    assert ".wav" in FORMATS and ".ape" not in FORMATS
    path = create_file(settings.music_path, "unsupported.txt")
    with pytest.raises(LibraryReadError, match="unsupported_extension"):
        LibraryScanner(session_factory, settings.music_path).index_one(path)


def test_diagnostics_bounded(caplog):
    summary = ScanDiagnostics(scan_id="scan-id")
    for i in range(110):
        summary.issue(f"unsafe\n{i}.wav", "malformed_audio")
    assert len(summary.samples) == 100 and summary.omitted_samples == 10
    assert all("\n" not in sample["relative_path"] for sample in summary.samples)
    assert len(caplog.records) == 100
    assert caplog.records[0].scan_id == "scan-id"
    assert caplog.records[0].relative_path == "unsafe?0.wav"
    assert caplog.records[0].reason == "malformed_audio"


def test_filename_fallback_shape():
    result = filename_metadata(Path("Artist/Album (2020)/Disc 02/03 - Song.wav"))
    assert result == {
        "artist": "Artist",
        "album": "Album",
        "year": 2020,
        "disc_number": 2,
        "track_number": 3,
        "title": "Song",
    }
    assert "artist" not in filename_metadata(Path("Genre/Artist/Album/Song.wav"))
    assert "artist" not in filename_metadata(Path("Folder/Song.wav"))


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_reader_rejects_path_replacement_after_open(monkeypatch, tmp_path, replacement):
    if os.name != "posix":
        pytest.skip("POSIX open-file replacement regression")
    path = create_file(tmp_path)
    before = path.stat()
    alternate = create_file(tmp_path, "alternate.mp3")
    os.utime(alternate, ns=(before.st_atime_ns, before.st_mtime_ns))

    def replace_path(*args, **kwargs):
        if replacement == "file":
            alternate.replace(path)
        else:
            path.unlink()
            path.symlink_to(alternate)
        return payload([{"codec_type": "audio", "codec_name": "mp3"}])

    monkeypatch.setattr(library_metadata, "MutagenFile", lambda *args, **kwargs: None)
    monkeypatch.setattr(library_metadata, "_probe", replace_path)
    with pytest.raises(LibraryReadError, match="file_changed"):
        library_metadata.read_audio_metadata(path, music_root=tmp_path)


@pytest.mark.parametrize("missing", [None, "artist", "title", "album", "unknown_artist"])
def test_native_metadata_reports_filename_fallback(monkeypatch, tmp_path, missing):
    path = create_file(
        tmp_path, "loose.mp3" if missing == "unknown_artist" else "Artist/Album/01 - Song.mp3"
    )
    tags = {"artist": ["Tagged Artist"], "title": ["Tagged Song"], "album": ["Tagged Album"]}
    if missing:
        tags.pop("artist" if missing == "unknown_artist" else missing)
    audio = type("MP3", (), {})()
    audio.info = SimpleNamespace(length=1.0, bitrate=128000)
    audio.tags = tags
    monkeypatch.setattr(library_metadata, "MutagenFile", lambda *args, **kwargs: audio)
    result = library_metadata.read_audio_metadata(path, music_root=tmp_path)
    assert result["_metadata_fallback"] is (missing is not None)
    if missing == "unknown_artist":
        assert result["artist"] == "Unknown Artist"


def payload(streams=None):
    return {
        "streams": streams or [{"codec_type": "audio", "codec_name": "aac"}],
        "format": {
            "duration": "2",
            "format_name": "mov,mp4,m4a",
            "tags": {
                "TITLE": "Song",
                "ARTIST": "Artist",
                "album": "Album",
                "MUSICBRAINZ_TRACKID": "11111111-1111-1111-1111-111111111111",
            },
        },
    }


def test_probe_tags_and_attached_art():
    data = payload(
        [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
        ]
    )
    result = probe_metadata(data, FORMATS[".m4a"])
    assert result["title"] == "Song" and result["codec"] == "aac"
    assert result["recording_mbid"] == "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize(
    "stream,reason",
    [
        ({"codec_type": "video", "codec_name": "h264"}, "video_bearing"),
        ({"codec_type": "audio", "codec_name": "not_supported"}, "unsupported_codec"),
        ({"codec_type": "attachment", "codec_name": "png"}, "no_audio"),
    ],
)
def test_probe_rejections(stream, reason):
    with pytest.raises(LibraryReadError, match=reason):
        probe_metadata(payload([stream]), FORMATS[".m4a"])


def test_probe_requires_tool_and_uses_local_only_policy(monkeypatch, tmp_path):
    path = create_file(tmp_path, "test.m4a")
    monkeypatch.setattr(library_metadata.shutil, "which", lambda _: None)
    with path.open("rb") as file, pytest.raises(LibraryReadError, match="probe_unavailable"):
        library_metadata._probe(file, path, FORMATS[".m4a"])


def test_library_mixed_pagination_filter_and_album_search(monkeypatch, session_factory, settings):
    monkeypatch.setattr(library_scan, "read_audio_metadata", metadata)
    for i in range(61):
        create_file(settings.music_path, f"{i:02}.{'mp3' if i % 2 else 'flac'}")
    LibraryScanner(session_factory, settings.music_path).run()
    repo = LibraryRepository(session_factory)
    assert repo.search("Album").total == 61
    assert len(repo.search(page=2, page_size=25).items) == 25
    assert repo.search(format="flac").total == 31
    assert repo.search().format_counts == {".flac": 31, ".mp3": 30}
    with session_factory.begin() as session:
        legacy = session.scalar(select(Track))
        legacy.file_extension = None
        legacy.codec = None
    assert repo.search(format="unknown", codec="unknown").total == 1


def test_audit_does_not_mutate_index(monkeypatch, session_factory, settings):
    from app.services import library_audit

    monkeypatch.setattr(library_audit, "read_audio_metadata", metadata)
    create_file(settings.music_path, "new.wav")
    result = audit_library(session_factory, settings.music_path, verbose=True)
    assert result["recognized_unindexed"] == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Track)) == 0
        assert session.scalar(select(func.count()).select_from(ScanRun)) == 0
