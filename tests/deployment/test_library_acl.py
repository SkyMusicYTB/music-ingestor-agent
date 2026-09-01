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


def test_write_probe_performs_operations_and_removes_its_file(tmp_path: Path) -> None:
    sentinel = tmp_path / "existing-track.mp3"
    sentinel.write_bytes(b"existing")

    result = run_probe("write", tmp_path)

    assert result.returncode == 0, result.stderr
    assert sentinel.read_bytes() == b"existing"
    assert list(tmp_path.glob(PROBE_GLOB)) == []


def test_read_probe_opens_and_lists_directory_without_mutating_it(tmp_path: Path) -> None:
    sentinel = tmp_path / "existing-track.mp3"
    sentinel.write_bytes(b"existing")

    result = run_probe("read", tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == [sentinel]


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
    assert "os.O_CREAT | os.O_EXCL" in source
    assert "secrets.token_hex(16)" in source
    assert "0o600, dir_fd=directory_fd" in source
    assert "os.unlink, probe_name, dir_fd=directory_fd" in source


def test_probe_rejects_a_symlink_instead_of_following_it(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    result = run_probe("write", link)

    assert result.returncode != 0
    assert list(target.iterdir()) == []
