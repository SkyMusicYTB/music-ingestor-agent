from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def active_requirements(path: str) -> list[str]:
    return [
        line.strip()
        for line in text(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_all_shell_scripts_parse_and_are_executable() -> None:
    scripts = sorted((REPO_ROOT / "scripts").rglob("*.sh"))
    assert scripts
    for script in scripts:
        subprocess.run(  # noqa: S603 - repository scripts are the test fixture
            ["/bin/bash", "-n", str(script)], check=True
        )
        if script.parent.name != "lib":
            assert os.access(script, os.X_OK), script
            contents = script.read_text(encoding="utf-8")
            assert contents.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")


def test_production_requirements_are_a_fully_pinned_closure() -> None:
    requirements = active_requirements("requirements/production.txt")
    assert len(requirements) >= 35
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+(?:\s+;\s+.+)?", item) for item in requirements
    )
    assert len({item.split("==", 1)[0].lower() for item in requirements}) == len(requirements)

    project = tomllib.loads(text("pyproject.toml"))
    direct = set(project["project"]["dependencies"])
    direct.update(project["build-system"]["requires"])
    input_pins = {
        line
        for line in active_requirements("requirements/production.in")
        if not line.startswith("-r")
    }
    assert direct == input_pins
    assert direct.issubset(set(requirements))


@pytest.mark.parametrize("name", ["production", "development"])
def test_hash_lock_matches_exact_closure(name: str) -> None:
    closure = {
        line.split("==", 1)[0].lower(): line.split("==", 1)[1]
        for line in active_requirements(f"requirements/{name}.txt")
    }
    lock = text(f"requirements/{name}.lock")
    starts = list(re.finditer(r"(?m)^([A-Za-z0-9_.-]+)==([^\\\s]+)[ ]+\\$", lock))
    locked = {match.group(1).lower(): match.group(2) for match in starts}
    assert locked == closure
    assert starts
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(lock)
        block = lock[match.start() : end]
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", block), match.group(1)


def test_tool_pins_are_official_and_have_real_digest_shapes() -> None:
    pins = dict(line.split("=", 1) for line in active_requirements("requirements/tool-pins.env"))
    assert pins["YT_DLP_URL"] == (
        f"https://github.com/yt-dlp/yt-dlp/releases/download/{pins['YT_DLP_VERSION']}/yt-dlp"
    )
    assert pins["DENO_URL"] == (
        f"https://github.com/denoland/deno/releases/download/"
        f"v{pins['DENO_VERSION']}/deno-x86_64-unknown-linux-gnu.zip"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", pins["YT_DLP_SHA256"])
    assert re.fullmatch(r"[0-9a-f]{64}", pins["DENO_SHA256"])


def test_environment_template_contains_no_secret_assignments() -> None:
    environment = text(".env.example")
    for secret in ("AUTH_HMAC_KEY", "OPENAI_API_KEY", "LISTENBRAINZ_TOKEN"):
        assert f"MUSIC_AGENT_{secret}=" not in environment
    assert "MUSIC_AGENT_BIND_HOST=0.0.0.0" in environment
    assert "MUSIC_AGENT_DATABASE_PATH=/var/lib/music-agent/music-agent.db" in environment
    assert "MUSIC_AGENT_MUSIC_PATH=/srv/music" in environment
    assert "CHANGE-ME@example.invalid" in environment


def test_systemd_credentials_and_write_boundaries() -> None:
    web = text("systemd/music-agent-web.service")
    worker = text("systemd/music-agent-worker.service")
    for unit in (web, worker):
        assert "User=music-agent" in unit
        assert "NoNewPrivileges=yes" in unit
        assert "CapabilityBoundingSet=\n" in unit
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=yes" in unit
        assert "PrivateDevices=yes" in unit
        assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in unit
        assert "Restart=on-failure" in unit
        assert "/var/lib/music-agent/backups" in unit
    assert "LoadCredential=auth_hmac_key:" in web
    assert "LoadCredential=openai_api_key:" in web
    assert "LoadCredential=listenbrainz_token:" in web
    assert "LoadCredential=" not in worker
    assert "Environment=MUSIC_AGENT_SERVICE_ROLE=web" in web
    assert "Environment=MUSIC_AGENT_SERVICE_ROLE=worker" in worker
    assert "ReadWritePaths=/var/lib/music-agent /var/lib/music-agent/artwork\n" in web
    assert "/srv/music-downloads /srv/music" in worker
    assert "ExecStartPre=/opt/music-agent/current/scripts/validate-runtime-environment.sh" in web


def test_library_acl_script_never_chowns_or_restarts_navidrome() -> None:
    acl_script = text("scripts/configure-library-acl.sh")
    assert not re.search(r"\bchown\b[^\n]*/srv/music", acl_script)
    assert not re.search(r"systemctl\s+(restart|stop|start)[^\n]*navidrome", acl_script)
    assert "getfacl --absolute-names --physical" in acl_script
    assert 'setfacl --modify "u:$MUSIC_AGENT_SERVICE_USER:rwX"' in acl_script
    assert "-xdev" in acl_script


def test_uninstall_explicitly_preserves_mutable_paths() -> None:
    uninstall = text("scripts/uninstall.sh")
    assert "--yes" in uninstall
    assert 'find "$MUSIC_AGENT_OPT_DIR" -depth -delete' in uninstall
    for variable in (
        "MUSIC_AGENT_MUSIC_DIR",
        "MUSIC_AGENT_DOWNLOAD_DIR",
        "MUSIC_AGENT_STATE_DIR",
        "MUSIC_AGENT_ETC_DIR",
    ):
        assert f'find "${variable}"' not in uninstall
        assert f'rm -rf "${variable}"' not in uninstall


def test_deployment_uses_backup_migration_and_atomic_link() -> None:
    deploy = text("scripts/deploy.sh")
    assert "git pull" in deploy
    assert "--only-binary=:all:" in deploy
    assert "--require-hashes" in deploy
    assert "hashed requirements/production.lock is required" in deploy
    assert "production.txt" not in deploy
    assert 'chmod 0640 "$MUSIC_AGENT_DB"' in deploy
    assert 'chmod 0600 "$MUSIC_AGENT_DB"' not in deploy
    assert "music_agent_stop_services" in deploy
    assert '--label "predeploy-$release_id"' in deploy
    assert '"$release/venv/bin/music-agent" migrate' in deploy
    assert 'music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"' in deploy
    assert "restore_previous_database" in deploy
    assert 'chmod -R a-w "$release"' in deploy


def test_activation_safety_backups_are_outside_service_owned_state() -> None:
    common = text("scripts/lib/common.sh")
    backup = text("scripts/backup.sh")
    web = text("systemd/music-agent-web.service")
    worker = text("systemd/music-agent-worker.service")

    assert "music-agent-safety-backups" in common
    assert 'install -d -m 0700 -o root -g root "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"' in common
    assert 'destination_dir="$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"' in backup
    assert 'chown root:root "$destination" "$destination.sha256" "$destination.json"' in backup
    assert 'music_agent_acquire_directory_lock "$MUSIC_AGENT_STATE_DIR" "backup"' in backup
    assert ".backup.lock" not in backup
    assert "exec 8>" not in backup
    for unit in (web, worker):
        assert "music-agent-safety-backups" not in unit
        assert "/var/lib/music-agent/acl-backups" not in unit
    for script_name in ("deploy.sh", "rollback.sh", "restore.sh"):
        assert "--protected" in text(f"scripts/{script_name}")


def test_deployment_never_materializes_web_credentials_while_worker_is_live() -> None:
    deploy = text("scripts/deploy.sh")
    validator = text("scripts/validate.sh")

    stop = deploy.index("music_agent_stop_services")
    first_credentials = deploy.index("music_agent_with_credentials")
    last_credentials = deploy.rindex("music_agent_with_credentials")
    start = deploy.rindex("music_agent_start_services")
    assert stop < first_credentials <= last_credentials < start
    assert "music_agent_with_credentials" not in validator
    assert "music_agent_without_credentials" in validator
    assert "--without-runtime-credentials" in validator
    common = text("scripts/lib/common.sh")
    guard = (
        "credential-backed administrative commands require music-agent-worker.service to be stopped"
    )
    assert common.index(guard) < common.index('credential_tmp="$(mktemp')


def test_native_operations_assert_the_exact_managed_production_layout() -> None:
    common = text("scripts/lib/common.sh")

    for assignment in (
        "MUSIC_AGENT_ENVIRONMENT=production",
        "MUSIC_AGENT_DATABASE_PATH=$MUSIC_AGENT_NATIVE_DATABASE_PATH",
        "MUSIC_AGENT_ARTWORK_PATH=$MUSIC_AGENT_NATIVE_ARTWORK_PATH",
        "MUSIC_AGENT_DOWNLOADS_PATH=$MUSIC_AGENT_NATIVE_DOWNLOADS_PATH",
        "MUSIC_AGENT_MUSIC_PATH=$MUSIC_AGENT_NATIVE_MUSIC_PATH",
        "MUSIC_AGENT_BACKUP_PATH=$MUSIC_AGENT_NATIVE_BACKUP_PATH",
    ):
        assert assignment in common
    for script_name in (
        "deploy.sh",
        "install.sh",
        "validate.sh",
        "configure-library-acl.sh",
        "backup.sh",
        "rollback.sh",
        "restore.sh",
    ):
        assert "music_agent_assert_managed_production_config" in text(f"scripts/{script_name}")


def test_live_database_permission_contract_is_0640() -> None:
    for script_name in ("deploy.sh", "restore.sh", "rollback.sh"):
        script = text(f"scripts/{script_name}")
        assert 'chmod 0640 "$MUSIC_AGENT_DB"' in script
        assert 'chmod 0600 "$MUSIC_AGENT_DB"' not in script
    validator = text("scripts/validate.sh")
    assert "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP:640" in validator
    assert (
        "database must be $MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP mode 0640"
        in validator
    )


def test_restore_permission_failures_enter_automatic_recovery() -> None:
    restore = text("scripts/restore.sh")

    assert "apply_database_permissions || restore_failed=1" in restore
    assert "elif ! apply_database_permissions; then" in restore
    assert (
        'chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"' in restore
    )
    assert 'chmod 0640 "$MUSIC_AGENT_DB"' in restore
    assert "automatic recovery was incomplete; services remain stopped" in restore

    helper_match = re.search(r"(?ms)^apply_database_permissions\(\) \{.*?^\}", restore)
    assert helper_match is not None
    result = subprocess.run(  # noqa: S603 - repository shell helper under controlled mocks
        [
            "/bin/bash",
            "-c",
            "set -e\n"
            "MUSIC_AGENT_SERVICE_USER=music-agent\nMUSIC_AGENT_SERVICE_GROUP=music-agent\n"
            "MUSIC_AGENT_DB=/tmp/test-music-agent.db\nchmod_called=0\n"
            "music_agent_warn() { :; }\n"
            "chown() { return 1; }\n"
            "chmod() { chmod_called=1; return 0; }\n"
            f"{helper_match.group(0)}\n"
            "restore_failed=0\n"
            "apply_database_permissions || restore_failed=1\n"
            'printf "%s %s\\n" "$restore_failed" "$chmod_called"\n',
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1 1\n"


@pytest.mark.parametrize(
    "script",
    [
        "install.sh",
        "deploy.sh",
        "rollback.sh",
        "restore.sh",
        "uninstall.sh",
        "set-secret.sh",
        "update-yt-dlp.sh",
        "backup.sh",
        "validate.sh",
    ],
)
def test_required_operational_scripts_exist(script: str) -> None:
    assert (REPO_ROOT / "scripts" / script).is_file()
