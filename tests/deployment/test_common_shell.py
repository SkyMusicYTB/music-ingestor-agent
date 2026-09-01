from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMON = REPO_ROOT / "scripts" / "lib" / "common.sh"
TOOLING = REPO_ROOT / "scripts" / "lib" / "tooling.sh"
RUNTIME_VALIDATOR = REPO_ROOT / "scripts" / "validate-runtime-environment.sh"


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
