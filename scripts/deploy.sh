#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly REPO_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: sudo scripts/deploy.sh [--allow-dirty] [--no-start]

Run after `git pull --ff-only`. By default a dirty Git worktree is rejected.
--no-start stages, migrates, and activates the release but leaves services stopped.
EOF
}

allow_dirty=0 start_services=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --allow-dirty) allow_dirty=1; shift ;;
        --no-start) start_services=0; shift ;;
        --from-install) shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done

music_agent_require_root
music_agent_assert_supported_host
music_agent_require_command git
music_agent_require_command rsync
music_agent_require_command flock
music_agent_require_command runuser
music_agent_acquire_lock operations

git_root="$(git -C "$REPO_DIR" rev-parse --show-toplevel 2>/dev/null)" ||
    music_agent_die "deployment source must be a Git checkout"
[[ "$(cd -- "$git_root" && pwd -P)" == "$REPO_DIR" ]] || music_agent_die "scripts must run from the repository root"
commit="$(git -C "$REPO_DIR" rev-parse --verify HEAD)"
dirty="$(git -C "$REPO_DIR" status --porcelain=v1 --untracked-files=normal)"
if [[ -n "$dirty" && "$allow_dirty" -ne 1 ]]; then
    music_agent_die "Git worktree is dirty; commit/stash changes or explicitly use --allow-dirty"
fi
source_suffix=""
[[ -z "$dirty" ]] || source_suffix="-dirty"
release_id="${commit:0:12}${source_suffix}-$(date -u +%Y%m%dT%H%M%SZ)"
[[ "$release_id" =~ ^[0-9a-f]{12}(-dirty)?-[0-9]{8}T[0-9]{6}Z$ ]] || music_agent_die "invalid release identifier"

install -d -m 0755 -o root -g root "$MUSIC_AGENT_RELEASES_DIR"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR"
staging="$MUSIC_AGENT_RELEASES_DIR/.staging-$release_id"
release="$MUSIC_AGENT_RELEASES_DIR/$release_id"
[[ ! -e "$staging" && ! -e "$release" ]] || music_agent_die "release already exists: $release_id"
install -d -m 0755 -o root -g root "$staging"
cleanup_staging() {
    if [[ -d "$staging" ]]; then
        music_agent_assert_within "$staging" "$MUSIC_AGENT_RELEASES_DIR"
        find "$staging" -depth -delete
    fi
}
trap cleanup_staging EXIT

music_agent_log "staging immutable release $release_id"
rsync --archive --delete --no-owner --no-group --safe-links \
    --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
    --exclude='/.git/' \
    --exclude='/.venv/' \
    --exclude='/venv/' \
    --exclude='/dev-data/' \
    --exclude='/data/' \
    --exclude='/downloads/' \
    --exclude='/music/' \
    --exclude='/backups/' \
    --exclude='/.pytest_cache/' \
    --exclude='/.mypy_cache/' \
    --exclude='/.ruff_cache/' \
    --exclude='/**/__pycache__/' \
    --exclude='/*.db*' \
    "$REPO_DIR/" "$staging/"

"$MUSIC_AGENT_PYTHON" -m venv "$staging/venv"
lock_file="$staging/requirements/production.lock"
[[ -f "$lock_file" ]] || music_agent_die "hashed requirements/production.lock is required"
"$staging/venv/bin/python" -m pip install --disable-pip-version-check \
    --require-hashes --only-binary=:all: --requirement "$lock_file"
"$staging/venv/bin/python" -m pip install --disable-pip-version-check \
    --no-build-isolation --no-deps "$staging"
"$staging/venv/bin/python" -m pip check
"$staging/venv/bin/python" -m compileall -q "$staging/app"
chmod 0755 "$staging/scripts/"*.sh "$staging/scripts/sqlite-maintenance.py"
schema_revision="$("$staging/venv/bin/python" -c 'from app.db.engine import EXPECTED_SCHEMA_REVISION; print(EXPECTED_SCHEMA_REVISION)')"
mv "$staging" "$release"
trap - EXIT

previous_release="$(music_agent_current_release)"
previous_web_active=0 previous_worker_active=0
if music_agent_unit_exists music-agent-web.service && music_agent_systemctl is-active --quiet music-agent-web.service; then
    previous_web_active=1
fi
if music_agent_unit_exists music-agent-worker.service && music_agent_systemctl is-active --quiet music-agent-worker.service; then
    previous_worker_active=1
fi

"$release/scripts/validate.sh" --release "$release" --pre-activate

install_release_units() {
    local unit source_release="${1:?release path required}"
    for unit in music-agent-web.service music-agent-worker.service music-agent-backup.service music-agent-backup.timer; do
        install -m 0644 -o root -g root "$source_release/systemd/$unit" "$MUSIC_AGENT_UNIT_DIR/$unit" || return 1
    done
    music_agent_systemctl daemon-reload
}

restore_previous_units() {
    local unit
    if [[ -n "$previous_release" && -d "$previous_release/systemd" ]]; then
        install_release_units "$previous_release"
    else
        for unit in music-agent-web.service music-agent-worker.service music-agent-backup.service music-agent-backup.timer; do
            find "$MUSIC_AGENT_UNIT_DIR" -maxdepth 1 -type f -name "$unit" -delete
        done
        music_agent_systemctl daemon-reload
    fi
}

database_existed=0 predeploy_backup=""
if ! music_agent_stop_services; then
    restore_previous_units || true
    [[ "$previous_web_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$previous_worker_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "could not stop both services; database was not touched"
fi
[[ -f "$MUSIC_AGENT_DB" ]] && database_existed=1
if [[ "$database_existed" -eq 1 ]]; then
    if ! predeploy_backup="$("$release/scripts/backup.sh" --label "predeploy-$release_id" --quiet)"; then
        restore_previous_units || true
        [[ "$previous_web_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
        [[ "$previous_worker_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
        music_agent_die "pre-deployment database backup failed; release was not activated"
    fi
fi

if ! install_release_units "$release"; then
    restore_previous_units || true
    [[ "$previous_web_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$previous_worker_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "could not install the staged systemd units; release was not activated"
fi

write_manifest() {
    local status="${1:?status required}" output="$release/RELEASE.json"
    "$MUSIC_AGENT_PYTHON" - "$output" "$release_id" "$commit" "$source_suffix" \
        "$schema_revision" "$status" "$previous_release" "$predeploy_backup" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

output, release_id, commit, source_suffix, schema, status, previous, backup = sys.argv[1:]
payload = {
    "format": 1,
    "release_id": release_id,
    "git_commit": commit,
    "dirty": bool(source_suffix),
    "schema_revision": schema,
    "status": status,
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "previous_release": previous or None,
    "predeploy_backup": backup or None,
}
Path(output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

restore_previous_database() {
    if [[ "$database_existed" -eq 1 && -n "$predeploy_backup" ]]; then
        "$MUSIC_AGENT_PYTHON" "$release/scripts/sqlite-maintenance.py" restore \
            --require-checksum --source "$predeploy_backup" --destination "$MUSIC_AGENT_DB" >/dev/null
        chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
        chmod 0640 "$MUSIC_AGENT_DB"
    elif [[ "$database_existed" -eq 0 && -f "$MUSIC_AGENT_DB" ]]; then
        failed_db="$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id-failed-new-database.db"
        mv "$MUSIC_AGENT_DB" "$failed_db"
        chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$failed_db"
        chmod 0640 "$failed_db"
        for suffix in -journal -wal -shm; do
            if [[ -e "$MUSIC_AGENT_DB$suffix" ]]; then
                mv "$MUSIC_AGENT_DB$suffix" "$failed_db$suffix"
                chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$failed_db$suffix"
                chmod 0640 "$failed_db$suffix"
            fi
        done
    fi
}

recover_activation() {
    local reason="${1:?reason required}" recovery_ok=1
    music_agent_warn "$reason; restoring the previous code/database pair"
    if ! music_agent_stop_services; then
        music_agent_die "$reason; automatic recovery could not stop all services, so no database restore was attempted"
    fi
    if [[ -n "$previous_release" ]]; then
        music_agent_atomic_symlink "$previous_release" "$MUSIC_AGENT_CURRENT_LINK"
    elif [[ -L "$MUSIC_AGENT_CURRENT_LINK" ]]; then
        unlink "$MUSIC_AGENT_CURRENT_LINK"
    fi
    restore_previous_database || recovery_ok=0
    restore_previous_units || recovery_ok=0
    write_manifest failed
    cp "$release/RELEASE.json" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
    chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
    chmod 0640 "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
    chown -R root:root "$release"
    chmod -R a-w "$release"
    if [[ "$recovery_ok" -eq 1 ]]; then
        if [[ "$previous_web_active" -eq 1 ]]; then music_agent_systemctl start music-agent-web.service || true; fi
        if [[ "$previous_worker_active" -eq 1 ]]; then music_agent_systemctl start music-agent-worker.service || true; fi
    else
        music_agent_warn "automatic database/unit restoration failed; services remain stopped"
    fi
    music_agent_die "$reason"
}

if ! music_agent_with_credentials "$release/venv/bin/music-agent" migrate; then
    recover_activation "database migration failed"
fi
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
    chmod 0640 "$MUSIC_AGENT_DB"
fi
if ! music_agent_with_credentials "$release/venv/bin/music-agent" validate; then
    recover_activation "post-migration validation failed"
fi
music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"
if ! "$release/scripts/validate.sh" --release "$release"; then
    recover_activation "activated release validation failed"
fi

if [[ "$start_services" -eq 1 ]]; then
    if ! music_agent_start_services; then
        recover_activation "service start failed"
    fi
    if ! "$release/scripts/validate.sh" --release "$release" --services; then
        recover_activation "post-activation validation failed"
    fi
fi

write_manifest active
cp "$release/RELEASE.json" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
chmod 0640 "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
chown -R root:root "$release"
chmod -R a-w "$release"
music_agent_log "release $release_id activated; pre-deployment backup: ${predeploy_backup:-not needed}"
