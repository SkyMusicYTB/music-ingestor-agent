from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON = REPO_ROOT / "scripts" / "lib" / "common.sh"
TOOLING = REPO_ROOT / "scripts" / "lib" / "tooling.sh"
RUNTIME_VALIDATOR = REPO_ROOT / "scripts" / "validate-runtime-environment.sh"
DIRECTORY_DENIAL_PROBE = REPO_ROOT / "scripts" / "lib" / "directory_write_denial_probe.py"


def run_bash(script: str, root: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "MUSIC_AGENT_TEST_MODE": "1",
        "MUSIC_AGENT_ROOT_PREFIX": str(root),
    }
    return subprocess.run(  # noqa: S603 - test-controlled shell fragments only
        ["/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_test_root_prefixes_every_managed_path(tmp_path: Path) -> None:
    result = run_bash(
        f'source "{COMMON}"; printf "%s\\n" "$MUSIC_AGENT_DB" "$MUSIC_AGENT_MUSIC_DIR"',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"{tmp_path}/var/lib/music-agent/music-agent.db",
        f"{tmp_path}/srv/music",
    ]


def test_environment_parser_accepts_only_music_agent_keys(tmp_path: Path) -> None:
    valid = tmp_path / "valid.env"
    valid.write_text(
        "# comment\nMUSIC_AGENT_BIND_HOST=127.0.0.1\n"
        'MUSIC_AGENT_MUSICBRAINZ_USER_AGENT="Music Agent/0.1"\n',
        encoding="utf-8",
    )
    result = run_bash(
        f'source "{COMMON}"; music_agent_parse_env_file "{valid}"; '
        'printf "%s\\n" "${MUSIC_AGENT_CONFIG_ENV[@]}"',
        tmp_path / "root",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "MUSIC_AGENT_BIND_HOST=127.0.0.1",
        "MUSIC_AGENT_MUSICBRAINZ_USER_AGENT=Music Agent/0.1",
    ]

    unsafe = tmp_path / "unsafe.env"
    unsafe.write_text("LD_PRELOAD=/tmp/evil.so\n", encoding="utf-8")
    result = run_bash(
        f'source "{COMMON}"; music_agent_parse_env_file "{unsafe}"',
        tmp_path / "root2",
    )
    assert result.returncode != 0
    assert "only MUSIC_AGENT_* variables" in result.stderr

    reserved = tmp_path / "reserved.env"
    reserved.write_text("MUSIC_AGENT_SERVICE_ROLE=worker\n", encoding="utf-8")
    result = run_bash(
        f'source "{COMMON}"; music_agent_parse_env_file "{reserved}"',
        tmp_path / "root3",
    )
    assert result.returncode != 0
    assert "managed by the systemd units" in result.stderr


def test_native_operations_reject_nonmanaged_production_paths(tmp_path: Path) -> None:
    environment = tmp_path / "music-agent.env"
    environment.write_text(
        "\n".join(
            (
                "MUSIC_AGENT_ENVIRONMENT=production",
                "MUSIC_AGENT_DATABASE_PATH=/var/lib/music-agent/music-agent.db",
                "MUSIC_AGENT_ARTWORK_PATH=/var/lib/music-agent/artwork",
                "MUSIC_AGENT_DOWNLOADS_PATH=/srv/music-downloads",
                "MUSIC_AGENT_MUSIC_PATH=/srv/music",
                "MUSIC_AGENT_BACKUP_PATH=/var/lib/music-agent/backups",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    command = (
        f'source "{COMMON}"; music_agent_parse_env_file "{environment}"; '
        "music_agent_assert_managed_production_config"
    )
    valid = run_bash(command, tmp_path / "valid-root")
    assert valid.returncode == 0, valid.stderr

    environment.write_text(
        environment.read_text(encoding="utf-8").replace(
            "/var/lib/music-agent/music-agent.db", "/var/lib/music-agent/custom.db"
        ),
        encoding="utf-8",
    )
    invalid = run_bash(command, tmp_path / "invalid-root")
    assert invalid.returncode != 0
    assert "must use the managed production value" in invalid.stderr


def test_transaction_backup_guard_uses_an_actual_account_operation() -> None:
    common = COMMON.read_text(encoding="utf-8")

    assert "directory_write_denial_probe.py" in common
    assert 'runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i' in common
    assert '< "$MUSIC_AGENT_DIRECTORY_WRITE_DENIAL_PROBE"' in common
    assert "test -w" not in common


def test_backup_lock_uses_directory_inode_without_touching_planted_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state = root / "var" / "lib" / "music-agent"
    backup = state / "backups"
    backup.mkdir(parents=True)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    (backup / ".backup.lock").symlink_to(sentinel)

    result = run_bash(
        f'source "{COMMON}"; flock() {{ return 0; }}; '
        'music_agent_acquire_directory_lock "$MUSIC_AGENT_STATE_DIR" backup',
        root,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert (backup / ".backup.lock").is_symlink()


def test_directory_inode_lock_rejects_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    physical = tmp_path / "physical"
    physical.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(physical, target_is_directory=True)

    result = run_bash(
        f'source "{COMMON}"; flock() {{ return 0; }}; '
        f'music_agent_acquire_directory_lock "{linked}" backup',
        root,
    )

    assert result.returncode != 0
    assert "must be a physical directory" in result.stderr


def test_credential_runner_refuses_live_worker_before_staging_secrets(tmp_path: Path) -> None:
    marker = tmp_path / "mktemp-called"
    result = run_bash(
        f'source "{COMMON}"; '
        "music_agent_unit_exists() { return 0; }; "
        "music_agent_systemctl() { return 0; }; "
        f'mktemp() {{ : > "{marker}"; return 1; }}; '
        "music_agent_with_credentials /bin/true",
        tmp_path / "root",
    )

    assert result.returncode != 0
    assert "require music-agent-worker.service to be stopped" in result.stderr
    assert not marker.exists()


def test_directory_write_denial_probe_attempts_and_cleans_real_io(tmp_path: Path) -> None:
    probe_name = ".music-agent-deny-write-0123456789abcdef0123456789abcdef"
    writable = subprocess.run(  # noqa: S603 - repository helper is the test fixture
        [sys.executable, str(DIRECTORY_DENIAL_PROBE), str(tmp_path), probe_name],
        check=False,
        capture_output=True,
        text=True,
    )

    assert writable.returncode != 0
    assert not (tmp_path / probe_name).exists()
    source = DIRECTORY_DENIAL_PROBE.read_text(encoding="utf-8")
    assert "os.O_CREAT | os.O_EXCL" in source
    assert "os.write" in source
    assert "os.fsync" in source
    assert "os.unlink" in source


def test_tool_pin_parser_does_not_source_shell(tmp_path: Path) -> None:
    pins = REPO_ROOT / "requirements" / "tool-pins.env"
    result = run_bash(
        f'source "{COMMON}"; source "{TOOLING}"; '
        f'music_agent_read_tool_pins "{pins}"; '
        'printf "%s %s\\n" "$YT_DLP_VERSION" "$DENO_VERSION"',
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2026.08.19 2.9.5"


def test_atomic_symlink_cleans_temporary_link_when_activation_fails(tmp_path: Path) -> None:
    link = tmp_path / "current"
    result = run_bash(
        f'source "{COMMON}"; '
        "mv() { return 1; }; "
        f'music_agent_atomic_symlink "/target/release" "{link}"',
        tmp_path / "root",
    )

    assert result.returncode != 0
    assert not link.is_symlink()
    assert list(tmp_path.glob(".current.new.*")) == []


def test_runtime_environment_rejects_placeholder_musicbrainz_contact() -> None:
    invalid = subprocess.run(  # noqa: S603 - repository script is the test fixture
        [str(RUNTIME_VALIDATOR)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "MUSIC_AGENT_MUSICBRAINZ_USER_AGENT": (
                "MusicAgent/0.1 (+mailto:CHANGE-ME@example.invalid)"
            )
        },
    )
    assert invalid.returncode != 0
    assert "placeholder MusicBrainz contact" in invalid.stderr

    valid = subprocess.run(  # noqa: S603 - repository script is the test fixture
        [str(RUNTIME_VALIDATOR)],
        check=False,
        capture_output=True,
        text=True,
        env={
            "MUSIC_AGENT_MUSICBRAINZ_USER_AGENT": (
                "MusicAgent/0.1 (+mailto:music-ops@real-domain.co.uk)"
            )
        },
    )
    assert valid.returncode == 0, valid.stderr
