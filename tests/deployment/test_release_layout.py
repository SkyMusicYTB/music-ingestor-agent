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

    create_release = 'install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$release"'
    create_venv = '"$MUSIC_AGENT_PYTHON" -m venv "$release/venv"'
    activate = 'music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"'
    assert deploy.index(create_release) < deploy.index(create_venv) < deploy.index(activate)
    assert ".staging-" not in deploy
    assert 'mv "$staging" "$release"' not in deploy
    assert '"$staging/venv' not in deploy

    assert '"$release/.build-incomplete"' in deploy
    assert "cleanup_incomplete_release" in deploy
    assert "write_manifest prepared" in deploy
    assert 'chown -R root:"$MUSIC_AGENT_SERVICE_GROUP" "$release"' in deploy
    assert 'chmod -R u=rwX,g=rX,o= "$release"' in deploy
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
    assert "music_agent_probe_tools_as_service" in validator
    config_check = (
        '"$release/venv/bin/music-agent" config-check --all-roles \\\n'
        "        --without-runtime-credentials >/dev/null"
    )
    assert config_check in validator
    assert validator.index(config_check) < validator.index('if [[ "$pre_activate" -eq 0 ]]')
    assert "music_agent_with_credentials" not in validator
    assert "music_agent_without_credentials" in validator
    deploy = repository_text("scripts/deploy.sh")
    assert (
        '"$release/scripts/validate.sh" --release "$release" --pre-activate --require-config-check'
    ) in deploy
    assert deploy.index("music_agent_stop_services") < deploy.index("music_agent_with_credentials")
    assert deploy.rindex("music_agent_with_credentials") < deploy.rindex(
        "music_agent_start_services"
    )


def test_git_is_run_only_as_the_physical_non_root_checkout_owner() -> None:
    deploy = repository_text("scripts/deploy.sh")

    assert 'repo_uid="$(stat -c \'%u\' "$REPO_DIR")"' in deploy
    assert '"$repo_uid" -ne 0' in deploy
    assert '"$(stat -c \'%u\' "$git_metadata")" == "$repo_uid"' in deploy
    assert "git_as_checkout_owner()" in deploy
    assert 'runuser -u "$repo_owner" -- env -i' in deploy
    assert '"GIT_OPTIONAL_LOCKS=0"' in deploy
    assert '"GIT_NO_REPLACE_OBJECTS=1"' in deploy
    assert '"GIT_INDEX_FILE=$owner_index"' in deploy
    assert 'chown "root:$passwd_gid" "$git_temp"' in deploy
    assert 'chmod 0710 "$git_temp"' in deploy
    assert 'install -m 0440 -o root -g "$passwd_gid" "$git_index" "$owner_index"' in deploy
    assert "-c core.fsmonitor=false" in deploy
    assert "verify_original_git_index" in deploy
    assert '"$(music_agent_sha256 "$git_index")" == "$git_index_digest"' in deploy
    assert "find \"$git_metadata\" -xdev -name '*.lock'" in deploy
    assert "deployment unexpectedly created a Git lock" in deploy
    assert 'git -C "$REPO_DIR"' not in deploy
    assert "$(git -C" not in deploy


def test_release_materialization_is_git_aware_and_excludes_ignored_files() -> None:
    deploy = repository_text("scripts/deploy.sh")

    assert "archive --format=tar" in deploy
    assert 'archive --format=tar "$commit" > "$source_archive"' in deploy
    assert '--output="$source_archive"' not in deploy
    assert "ls-files -z --cached --others --exclude-standard" in deploy
    assert '--from0 --files-from="$source_manifest"' in deploy
    assert "--safe-links" in deploy
    assert 'mode == b"160000"' in deploy
    assert "Git submodules are not supported" in deploy
    assert "tracked symlinks are not supported" in deploy
    assert 'name == ".env"' in deploy
    assert 'name.endswith((".pem", ".key"))' in deploy
    assert '"source_manifest_sha256": source_manifest_sha256' in deploy
    assert '"$SOURCE_SNAPSHOT"' in deploy
    assert '"$source_content_before" "$source_state_before"' in deploy
    assert 'cmp -s "$source_content_before" "$release_content"' in deploy
    assert 'cmp -s "$source_content_before" "$source_content_after"' in deploy
    assert 'cmp -s "$source_state_before" "$source_state_after"' in deploy
    assert "--exclude='/.git/'" not in deploy


def test_source_snapshot_detects_content_and_metadata_changes(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(REPO_ROOT / "scripts/lib/source_snapshot.py"))
    build = cast(Callable[[Path, Path], tuple[bytes, bytes]], namespace["build_snapshots"])
    root = tmp_path / "source"
    root.mkdir()
    tracked = root / "tracked.txt"
    tracked.write_text("first", encoding="utf-8")
    manifest = tmp_path / "manifest"
    manifest.write_bytes(b"tracked.txt\0")

    first_content, first_state = build(root, manifest)
    tracked.write_text("other", encoding="utf-8")
    second_content, second_state = build(root, manifest)

    assert first_content != second_content
    assert first_state != second_state


def test_source_snapshot_rejects_symlink_components(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(REPO_ROOT / "scripts/lib/source_snapshot.py"))
    build = cast(Callable[[Path, Path], tuple[bytes, bytes]], namespace["build_snapshots"])
    root = tmp_path / "source"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret").write_text("not deployable", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    manifest = tmp_path / "manifest"
    manifest.write_bytes(b"linked/secret\0")

    with pytest.raises(OSError):
        build(root, manifest)


def test_release_and_tool_trees_have_explicit_runtime_access_modes() -> None:
    deploy = repository_text("scripts/deploy.sh")
    validator = repository_text("scripts/validate.sh")
    tooling = repository_text("scripts/lib/tooling.sh")

    assert 'chmod -R u=rwX,g=rX,o= "$release"' in deploy
    assert (
        'install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_RELEASES_DIR"'
    ) in deploy
    assert 'group "$MUSIC_AGENT_SERVICE_GROUP"' in validator
    assert 'find "$release" -xdev ! -type l -perm /007' in validator
    assert "music_agent_normalize_tool_tree" in deploy
    assert 'install -d -m 0755 -o root -g root "$directory"' in tooling
    assert 'chmod 0755 "$executable"' in tooling
    assert 'chmod 0644 "$file"' in tooling
    assert "unexpected content in managed tool version" in tooling
    assert "managed tool bin contains an unexpected entry" in tooling


def test_tool_update_never_leaves_an_unvalidated_link_active() -> None:
    updater = repository_text("scripts/update-yt-dlp.sh")

    assert "music_agent_probe_tools_as_service" in updater
    assert updater.count('unlink "$MUSIC_AGENT_TOOL_BIN/yt-dlp"') >= 2
    assert 'music_agent_atomic_symlink "$old_target" "$MUSIC_AGENT_TOOL_BIN/yt-dlp"' in updater
    assert 'chown -h root:root "$MUSIC_AGENT_TOOL_BIN/yt-dlp"' in updater


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
    assert "music_agent_with_credentials" not in admin
    assert "music_agent_without_credentials" in admin
    assert '"$release/venv/bin/music-agent" migrate' in deploy
    assert '"$release/venv/bin/music-agent" validate' in deploy
    assert '"$current_release/venv/bin/music-agent" migrate' in restore
    assert '"$current_release/venv/bin/music-agent" validate' in restore
    assert '"$target_release/venv/bin/music-agent" validate' in rollback
