from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def repository_text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_deploy_builds_virtualenv_at_final_nonrelocated_path() -> None:
    deploy = repository_text("scripts/deploy.sh")

    create_release = 'install -d -m 0755 -o root -g root "$release"'
    create_venv = '"$MUSIC_AGENT_PYTHON" -m venv "$release/venv"'
    activate = 'music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"'
    assert deploy.index(create_release) < deploy.index(create_venv) < deploy.index(activate)
    assert ".staging-" not in deploy
    assert 'mv "$staging" "$release"' not in deploy
    assert '"$staging/venv' not in deploy

    assert '"$release/.build-incomplete"' in deploy
    assert "cleanup_incomplete_release" in deploy
    assert "write_manifest prepared" in deploy
    assert 'chown -R root:root "$release"' in deploy
    assert 'chmod -R u=rwX,go=rX "$release"' in deploy
    assert 'chmod -R a-w "$release"' in deploy


def test_pre_activation_probe_executes_both_entry_points_as_service_user() -> None:
    validator = repository_text("scripts/validate.sh")
    probe_call = '"$release/venv/bin/python" "$RELEASE_ACCESS_PROBE" "$release"'

    assert validator.count('runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i') >= 3
    assert probe_call in validator
    assert '"$release/venv/bin/music-agent" --help' in validator
    assert '"$release/venv/bin/music-agent-worker" --help' in validator
    assert 'find "$release" -xdev ! -type l -perm /022' in validator
    assert 'find "$release" -xdev ! -type l -perm /222' in validator


def test_rollback_preflights_target_before_stopping_services() -> None:
    rollback = repository_text("scripts/rollback.sh")
    preflight = '"$SCRIPT_DIR/validate.sh" --release "$target_release" --pre-activate'
    untrusted_preflight = (
        '"$target_release/scripts/validate.sh" --release "$target_release" --pre-activate'
    )
    assert rollback.index(preflight) < rollback.index("music_agent_stop_services")
    assert untrusted_preflight not in rollback
    assert '"$SCRIPT_DIR/validate.sh" --release "$target_release" --services' in rollback


def test_post_migration_mutations_are_guarded_by_activation_recovery() -> None:
    deploy = repository_text("scripts/deploy.sh")

    assert (
        'if ! chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB" ||'
        in deploy
    )
    assert 'recover_activation "could not secure the migrated database"' in deploy
    assert 'if ! music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"; then' in deploy
    assert 'recover_activation "could not activate the candidate release symlink"' in deploy
    assert 'recover_activation "could not record the successful deployment"' in deploy


def test_rollback_state_mutations_are_explicitly_failure_checked() -> None:
    rollback = repository_text("scripts/rollback.sh")

    assert '! music_agent_atomic_symlink "$target_release" "$MUSIC_AGENT_CURRENT_LINK"' in rollback
    assert '! install_release_units "$target_release"' in rollback
    assert (
        'music_agent_atomic_symlink "$current_release" "$MUSIC_AGENT_CURRENT_LINK" || recovery_ok=0'
        in rollback
    )
    assert 'install_release_units "$current_release" || recovery_ok=0' in rollback
    assert 'database_schema="" database_existed=0' in rollback
    assert "archive_new_database || recovery_ok=0" in rollback


def test_console_script_probe_rejects_relocated_shebang(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(REPO_ROOT / "scripts/lib/release_access_probe.py"))
    validate = cast(Callable[[Path], None], namespace["validate_console_scripts"])
    release = tmp_path / "release"
    bin_dir = release / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    expected = f"#!{bin_dir / 'python'}\n"
    for name in ("music-agent", "music-agent-worker", "alembic"):
        (bin_dir / name).write_text(expected, encoding="utf-8")

    validate(release)

    (bin_dir / "alembic").write_text(
        "#!/opt/music-agent/releases/.staging-example/venv/bin/python\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="staging path"):
        validate(release)


def test_console_script_probe_rejects_a_moved_virtualenv(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(REPO_ROOT / "scripts/lib/release_access_probe.py"))
    validate = cast(Callable[[Path], None], namespace["validate_console_scripts"])
    original = tmp_path / "original"
    bin_dir = original / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    expected = f"#!{bin_dir / 'python'}\n"
    for name in ("music-agent", "music-agent-worker"):
        (bin_dir / name).write_text(expected, encoding="utf-8")

    moved = tmp_path / "moved"
    original.rename(moved)

    with pytest.raises(RuntimeError, match="does not point to the final release interpreter"):
        validate(moved)


def test_service_invocations_use_active_or_explicit_release_paths() -> None:
    web = repository_text("systemd/music-agent-web.service")
    worker = repository_text("systemd/music-agent-worker.service")
    admin = repository_text("scripts/music-agentctl.sh")
    deploy = repository_text("scripts/deploy.sh")
    restore = repository_text("scripts/restore.sh")
    rollback = repository_text("scripts/rollback.sh")

    assert "ExecStartPre=/opt/music-agent/current/venv/bin/music-agent validate" in web
    assert "ExecStart=/opt/music-agent/current/venv/bin/music-agent web" in web
    assert "ExecStart=/opt/music-agent/current/venv/bin/music-agent-worker" in worker
    assert '"$current_release/venv/bin/music-agent" "${arguments[@]}"' in admin
    assert '"$release/venv/bin/music-agent" migrate' in deploy
    assert '"$release/venv/bin/music-agent" validate' in deploy
    assert '"$current_release/venv/bin/music-agent" migrate' in restore
    assert '"$current_release/venv/bin/music-agent" validate' in restore
    assert '"$target_release/venv/bin/music-agent" validate' in rollback
