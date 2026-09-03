from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.db.models import Track
from app.services import library_scan
from app.services.library_scan import LibraryScanner


def test_incremental_scan_does_not_reread_unchanged_tags(
    monkeypatch, session_factory, settings
) -> None:
    paths = [
        settings.music_path / "Artist" / "Album" / f"0{number} - Song.mp3" for number in range(1, 4)
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    calls: list[Path] = []

    def fake_metadata(path: Path, *, music_root: Path | None = None) -> dict[str, object]:
        calls.append(path)
        return {
            "artist": "Artist",
            "title": path.stem.split(" - ", 1)[1],
            "album": "Album",
            "album_artist": "Artist",
            "genre": None,
            "year": 2026,
            "track_number": int(path.stem[:2]),
            "track_total": 3,
            "disc_number": 1,
            "disc_total": 1,
            "duration_seconds": 180.0,
            "codec": "synthetic",
            "bitrate": None,
            "recording_mbid": None,
            "release_mbid": None,
            "release_group_mbid": None,
            "source_extractor": None,
            "source_id": None,
            "source_url": None,
            "version_signature": "studio",
        }

    monkeypatch.setattr(library_scan, "read_audio_metadata", fake_metadata)
    scanner = LibraryScanner(session_factory, settings.music_path)
    first = scanner.run()
    second = scanner.run()
    assert first.kind == "initial"
    assert first.changed_files == 3
    assert second.kind == "incremental"
    assert second.changed_files == 0
    assert len(calls) == 3
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Track)) == 3


def test_scan_never_follows_symlinked_files(
    monkeypatch, session_factory, settings, tmp_path
) -> None:
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    link = settings.music_path / "linked.mp3"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    monkeypatch.setattr(
        library_scan,
        "read_audio_metadata",
        lambda _path: (_ for _ in ()).throw(AssertionError("symlink was followed")),
    )
    scan = LibraryScanner(session_factory, settings.music_path).run()
    assert scan.scanned_files == 0


def test_mp4_track_and_disc_tuple_atoms_are_preserved() -> None:
    assert library_scan._first({"trkn": [(2, 9)]}, "trkn") == "2/9"
    assert library_scan._first({"disk": [(1, 2)]}, "disk") == "1/2"
