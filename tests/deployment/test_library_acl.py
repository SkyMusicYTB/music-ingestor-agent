from __future__ import annotations

import runpy
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ACL_SCRIPT = REPO_ROOT / "scripts" / "configure-library-acl.sh"
ACCESS_PROBE = REPO_ROOT / "scripts" / "lib" / "library_access_probe.py"
ACL_GUARD = REPO_ROOT / "scripts" / "lib" / "acl_snapshot_guard.py"
PROBE_GLOB = ".music-agent-acl-probe-*.tmp"


def run_probe(operation: str, directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - repository helper is the test fixture
        [sys.executable, str(ACCESS_PROBE), operation, str(directory)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_acl_script_uses_real_target_account_operations() -> None:
    script = ACL_SCRIPT.read_text(encoding="utf-8")

    assert not any(
        fragment in script
        for fragment in (
            '-- test -r "$MUSIC_AGENT_MUSIC_DIR"',
            '-- test -w "$MUSIC_AGENT_MUSIC_DIR"',
        )
    )
    assert 'verify_account_access "$MUSIC_AGENT_SERVICE_USER" write' in script
    assert 'verify_account_access "$navidrome_user" read' in script
    assert 'runuser -u "$account" -- "$MUSIC_AGENT_PYTHON" -' in script
    assert '< "$ACL_ACCESS_PROBE"' in script


def test_acl_script_refuses_ambiguous_navidrome_auto_detection() -> None:
    script = ACL_SCRIPT.read_text(encoding="utf-8")

    assert "mapfile -t navidrome_units" in script
    assert '"${#navidrome_units[@]}" -gt 1' in script
    assert "multiple Navidrome units detected" in script
    assert 'navidrome_unit="${navidrome_units[0]}"' in script


def test_acl_mutation_rolls_back_and_does_not_force_masks() -> None:
    script = ACL_SCRIPT.read_text(encoding="utf-8")

    assert "getfacl --absolute-names --physical --all-effective" in script
    assert "music_agent_prepare_transaction_backup_dir" in script
    assert 'acl_rollback_dir="$MUSIC_AGENT_TRANSACTION_BACKUP_DIR/acl"' in script
    assert 'acl_backup="$acl_rollback_dir/$acl_snapshot_name"' in script
    assert 'setfacl --restore="$acl_backup"' in script
    assert "$MUSIC_AGENT_STATE_DIR/acl-backups" not in script
    assert 'unlink "$acl_backup"' not in script.split("acl_rollback_required=1", 1)[1]
    assert "trap rollback_acl_on_exit EXIT" in script
    assert "acl_rollback_required=0" in script
    assert "d:m::rwx" not in script
    assert '"$MUSIC_AGENT_PYTHON" "$ACL_SNAPSHOT_GUARD"' in script


def test_acl_guard_rejects_dormant_permissions_for_unchanged_principals(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ACL_GUARD))
    validate_snapshot = cast(Callable[[Path, frozenset[str]], None], namespace["validate_snapshot"])
    snapshot = tmp_path / "snapshot.acl"
    snapshot.write_text(
        "# file: /srv/music\n"
        "# owner: root\n"
        "# group: media\n"
        "user::rwx\n"
        "user:music-agent:rwx\t#effective:r-x\n"
        "group::rwx\t#effective:r-x\n"
        "mask::r-x\n"
        "other::r-x\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="refusing to widen its mask"):
        validate_snapshot(snapshot, frozenset({"music-agent"}))

    snapshot.write_text(
        snapshot.read_text(encoding="utf-8").replace(
            "group::rwx\t#effective:r-x", "group::r-x\t#effective:r-x"
        ),
        encoding="utf-8",
    )
    validate_snapshot(snapshot, frozenset({"music-agent"}))


def test_write_probe_performs_operations_and_removes_its_file(tmp_path: Path) -> None:
    sentinel = tmp_path / "existing-track.mp3"
    sentinel.write_bytes(b"existing")

    result = run_probe("write", tmp_path)

    assert result.returncode == 0, result.stderr
    assert sentinel.read_bytes() == b"existing"
    assert list(tmp_path.glob(PROBE_GLOB)) == []


def test_read_probe_opens_and_lists_directory_without_mutating_it(tmp_path: Path) -> None:
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    sentinel = album / "existing-track.mp3"
    sentinel.write_bytes(b"existing")

    result = run_probe("read", tmp_path)

    assert result.returncode == 0, result.stderr
    assert sentinel.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [album.parent]


def test_write_probe_cleans_up_when_operation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(ACCESS_PROBE))
    probe_os = cast(Any, namespace["os"])
    verify_write = cast(Callable[[Path], None], namespace["verify_write"])

    def fail_after_create(_file_descriptor: int) -> None:
        raise OSError("injected fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(probe_os, "fsync", fail_after_create)
        with pytest.raises(OSError, match="injected fsync failure"):
            verify_write(tmp_path)

    assert list(tmp_path.glob(PROBE_GLOB)) == []


def test_probe_uses_directory_descriptors_and_exclusive_secure_creation() -> None:
    source = ACCESS_PROBE.read_text(encoding="utf-8")

    assert "os.O_DIRECTORY" in source
    assert "os.O_NOFOLLOW" in source
    assert "os.listdir(directory_fd)" in source
    assert "os.mkdir(candidate, 0o770" in source
    assert "os.O_CREAT | os.O_EXCL" in source
    assert "secrets.token_hex(16)" in source
    assert "0o660, dir_fd=child_fd" in source
    assert "os.unlink, PROBE_FILE, dir_fd=child_fd" in source
    assert "os.rmdir, probe_name, dir_fd=directory_fd" in source


def test_read_probe_opens_an_existing_nested_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    namespace = runpy.run_path(str(ACCESS_PROBE))
    probe_os = cast(Any, namespace["os"])
    verify_read = cast(Callable[[Path], None], namespace["verify_read"])
    nested = tmp_path / "Artist" / "Album"
    nested.mkdir(parents=True)
    track = nested / "track.opus"
    track.write_bytes(b"audio")
    opened: list[str | bytes | int] = []
    real_open = probe_os.open

    def recording_open(path: str | bytes | int, *args: Any, **kwargs: Any) -> int:
        opened.append(path)
        return cast(int, real_open(path, *args, **kwargs))

    monkeypatch.setattr(probe_os, "open", recording_open)
    verify_read(tmp_path)

    assert track.name in opened


def test_probe_rejects_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    result = run_probe("write", link)

    assert result.returncode != 0
    assert list(target.iterdir()) == []
