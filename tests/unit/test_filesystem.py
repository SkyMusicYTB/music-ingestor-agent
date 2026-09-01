from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from app.services.filesystem import (
    DestinationExistsError,
    UnsafePathError,
    add_source_collision_suffix,
    build_track_relative_path,
    create_staging_directory,
    publish_album_cover_no_clobber,
    publish_no_clobber,
)


def test_atomic_publication_is_complete_and_does_not_clobber(tmp_path: Path) -> None:
    source = tmp_path / "staging.m4a"
    source.write_bytes(b"complete-media")
    root = tmp_path / "music"
    result = publish_no_clobber(source, root, "Artist/Album/01 - Track.m4a")
    assert result.path.read_bytes() == b"complete-media"
    assert result.sha256 == hashlib.sha256(b"complete-media").hexdigest()
    assert source.exists()

    replacement = tmp_path / "replacement.m4a"
    replacement.write_bytes(b"different")
    with pytest.raises(DestinationExistsError):
        publish_no_clobber(replacement, root, result.relative_path)
    assert result.path.read_bytes() == b"complete-media"


def test_publication_rejects_symlinked_destination_component(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"media")
    root = tmp_path / "music"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "Artist").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        publish_no_clobber(source, root, "Artist/Album/Track.mp3")
    assert not (outside / "Album" / "Track.mp3").exists()


@pytest.mark.parametrize("path", ["../escape.mp3", "/absolute.mp3", "a\\..\\escape.mp3"])
def test_publication_rejects_unsafe_relative_paths(tmp_path: Path, path: str) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"media")
    with pytest.raises(UnsafePathError):
        publish_no_clobber(source, tmp_path / "music", path)


def test_track_path_sanitizes_provider_metadata() -> None:
    result = build_track_relative_path(
        artist="../Artist",
        album="Album/../../escape",
        title="Song: live\\take",
        track_number=1,
        extension=".M4A",
        year=2026,
        disc_number=1,
        disc_total=2,
    )
    assert result == "_Artist/Album_.._.._escape (2026)/Disc 01/01 - Song_ live_take.m4a"
    assert "../" not in result


def test_layout_protects_reserved_names_and_has_stable_collision_suffix() -> None:
    result = build_track_relative_path(
        artist="CON",
        album="NUL",
        year=1999,
        disc_number=2,
        disc_total=2,
        title="Track",
        track_number=3,
        extension="opus",
    )
    assert result == "_CON/NUL (1999)/Disc 02/03 - Track.opus"
    assert add_source_collision_suffix(result, "dQw4w9WgXcQ").endswith(
        "03 - Track [dQw4w9WgXcQ].opus"
    )


def test_collision_suffix_retruncates_a_maximum_length_title() -> None:
    original = build_track_relative_path(
        artist="Artist",
        album="Album",
        title="é" * 200,
        track_number=1,
        extension="flac",
    )

    collided = add_source_collision_suffix(original, "source-id-" + "x" * 80)

    assert len(Path(collided).name.encode()) <= 255
    assert "[source-id-" in Path(collided).name


def test_cover_sidecar_is_no_clobber(tmp_path: Path) -> None:
    root = tmp_path / "music"
    first = publish_album_cover_no_clobber(b"jpeg-one", root, "Artist/Album (2026)")
    assert first.relative_path == "Artist/Album (2026)/cover.jpg"
    with pytest.raises(DestinationExistsError):
        publish_album_cover_no_clobber(b"jpeg-two", root, "Artist/Album (2026)")
    assert first.path.read_bytes() == b"jpeg-one"


def test_staging_directory_is_stable_private_and_reused_for_retry(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    job_id = "11111111-1111-1111-1111-111111111111"
    first = create_staging_directory(root, job_id)
    partial = first / "youtube-source.webm.part"
    partial.write_bytes(b"resumable")

    second = create_staging_directory(root, job_id)

    assert second == first
    assert partial.read_bytes() == b"resumable"
    assert stat.S_IMODE(second.stat().st_mode) == 0o700
