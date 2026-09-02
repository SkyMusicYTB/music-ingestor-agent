#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly REPO_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=scripts/lib/tooling.sh
source "$SCRIPT_DIR/lib/tooling.sh"
readonly SOURCE_SNAPSHOT="$SCRIPT_DIR/lib/source_snapshot.py"

usage() {
    cat <<'EOF'
Usage: sudo scripts/deploy.sh [--allow-dirty] [--no-start]

Run after `git pull --ff-only`. By default a dirty Git worktree is rejected.
--no-start builds, migrates, and activates the release but leaves services stopped.
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
music_agent_require_command getent
music_agent_require_command cmp
music_agent_require_command rsync
music_agent_require_command flock
music_agent_require_command runuser
music_agent_require_command stat
music_agent_require_command tar
[[ -r "$SOURCE_SNAPSHOT" ]] || music_agent_die "source snapshot helper is missing: $SOURCE_SNAPSHOT"
music_agent_acquire_lock operations
music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
music_agent_assert_managed_production_config

[[ ! -L "$REPO_DIR" && -d "$REPO_DIR" ]] ||
    music_agent_die "deployment source must be a physical directory"
git_metadata="$REPO_DIR/.git"
[[ -d "$git_metadata" && ! -L "$git_metadata" ]] ||
    music_agent_die "deployment source must use a physical .git directory; worktrees and gitfiles are not supported"
git_index="$git_metadata/index"
[[ -f "$git_index" && ! -L "$git_index" ]] ||
    music_agent_die "deployment source has no regular Git index"

repo_uid="$(stat -c '%u' "$REPO_DIR")"
repo_gid="$(stat -c '%g' "$REPO_DIR")"
[[ "$repo_uid" =~ ^[0-9]+$ && "$repo_gid" =~ ^[0-9]+$ && "$repo_uid" -ne 0 ]] ||
    music_agent_die "deployment checkout must be owned by a non-root administrator"
repo_passwd="$(getent passwd "$repo_uid")"
[[ -n "$repo_passwd" ]] || music_agent_die "checkout owner UID $repo_uid has no local account"
IFS=: read -r repo_owner _password passwd_uid passwd_gid _gecos repo_home _shell <<< "$repo_passwd"
[[ "$passwd_uid" == "$repo_uid" && "$passwd_gid" =~ ^[0-9]+$ && "$repo_owner" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] ||
    music_agent_die "checkout owner account is invalid"
[[ "$repo_home" == /* && -d "$repo_home" ]] || music_agent_die "checkout owner has no usable home directory"
[[ "$(stat -c '%u' "$git_metadata")" == "$repo_uid" ]] ||
    music_agent_die "the repository and its .git directory must have the same non-root owner"
[[ -z "$(find "$git_metadata" -maxdepth 0 -perm /022 -print -quit)" ]] ||
    music_agent_die "the Git metadata directory must not be writable by group or other users"
index_uid="$(stat -c '%u' "$git_index")"
[[ "$index_uid" == "$repo_uid" || "$index_uid" == "0" ]] ||
    music_agent_die "the Git index has an unexpected owner"
[[ -z "$(find "$git_index" -maxdepth 0 -perm /022 -print -quit)" ]] ||
    music_agent_die "the Git index must not be writable by group or other users"
[[ -z "$(find "$git_metadata" -xdev -name '*.lock' -print -quit)" ]] ||
    music_agent_die "the Git checkout already contains a lock"

git_index_stat="$(stat -c '%u:%g:%a:%s:%y' "$git_index")"
git_index_digest="$(music_agent_sha256 "$git_index")"
git_temp="$(mktemp -d)"
# Git receives a read-only index copy, while the root-owned scratch directory
# prevents the checkout owner (or a concurrent process under that account) from
# replacing manifests or archives after Git has written them through root-opened
# stdout descriptors.
chown "root:$passwd_gid" "$git_temp"
chmod 0710 "$git_temp"
owner_index="$git_temp/index"
install -m 0440 -o root -g "$passwd_gid" "$git_index" "$owner_index"

# shellcheck disable=SC2329 # invoked indirectly by the EXIT trap
cleanup_git_snapshot() {
    if [[ -n "${git_temp:-}" && -d "$git_temp" ]]; then
        find "$git_temp" -xdev -depth -delete
    fi
}
trap cleanup_git_snapshot EXIT

git_as_checkout_owner() {
    runuser -u "$repo_owner" -- env -i \
        "HOME=$repo_home" \
        "PATH=/usr/bin:/bin" \
        "GIT_CONFIG_NOSYSTEM=1" \
        "GIT_CONFIG_GLOBAL=/dev/null" \
        "GIT_OPTIONAL_LOCKS=0" \
        "GIT_NO_REPLACE_OBJECTS=1" \
        "GIT_TERMINAL_PROMPT=0" \
        "GIT_INDEX_FILE=$owner_index" \
        git -c core.fsmonitor=false -c core.hooksPath=/dev/null -C "$REPO_DIR" "$@"
}

verify_original_git_index() {
    [[ -f "$git_index" && ! -L "$git_index" ]] ||
        music_agent_die "the original Git index changed type during deployment"
    [[ "$(stat -c '%u:%g:%a:%s:%y' "$git_index")" == "$git_index_stat" ]] ||
        music_agent_die "the original Git index metadata changed during deployment"
    [[ "$(music_agent_sha256 "$git_index")" == "$git_index_digest" ]] ||
        music_agent_die "the original Git index content changed during deployment"
    [[ -z "$(find "$git_metadata" -xdev -name '*.lock' -print -quit)" ]] ||
        music_agent_die "deployment unexpectedly created a Git lock"
}

git_root="$(git_as_checkout_owner rev-parse --show-toplevel 2>/dev/null)" ||
    music_agent_die "deployment source must be a Git checkout"
[[ "$(cd -- "$git_root" && pwd -P)" == "$REPO_DIR" ]] ||
    music_agent_die "scripts must run from the repository root"
commit="$(git_as_checkout_owner rev-parse --verify 'HEAD^{commit}')"
[[ "$commit" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || music_agent_die "Git returned an invalid commit ID"

status_before="$git_temp/status-before"
git_as_checkout_owner status --porcelain=v1 -z --untracked-files=normal > "$status_before"
if [[ -s "$status_before" && "$allow_dirty" -ne 1 ]]; then
    music_agent_die "Git worktree is dirty; commit/stash changes or explicitly use --allow-dirty"
fi
source_suffix=""
[[ ! -s "$status_before" ]] || source_suffix="-dirty"
release_id="${commit:0:12}${source_suffix}-$(date -u +%Y%m%dT%H%M%SZ)"
[[ "$release_id" =~ ^[0-9a-f]{12}(-dirty)?-[0-9]{8}T[0-9]{6}Z$ ]] || music_agent_die "invalid release identifier"

install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_RELEASES_DIR"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR"
release="$MUSIC_AGENT_RELEASES_DIR/$release_id"
[[ ! -e "$release" ]] || music_agent_die "release already exists: $release_id"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$release"
install -m 0644 -o root -g root /dev/null "$release/.build-incomplete"
# shellcheck disable=SC2329 # invoked indirectly by the EXIT trap
cleanup_incomplete_release() {
    cleanup_git_snapshot
    if [[ -f "$release/.build-incomplete" ]]; then
        music_agent_assert_within "$release" "$MUSIC_AGENT_RELEASES_DIR"
        find "$release" -xdev -depth -delete
    fi
}
trap cleanup_incomplete_release EXIT

music_agent_log "building inactive release $release_id at its final path"
tracked_entries="$git_temp/tracked-entries"
git_as_checkout_owner ls-files --stage -z > "$tracked_entries"
"$MUSIC_AGENT_PYTHON" - "$tracked_entries" <<'PY'
import sys
from pathlib import Path

entries = Path(sys.argv[1]).read_bytes().split(b"\0")
for entry in entries:
    if not entry:
        continue
    header, separator, raw_path = entry.partition(b"\t")
    fields = header.split()
    if not separator or len(fields) != 3:
        raise SystemExit("invalid Git index entry")
    mode, _object_id, stage = fields
    if stage != b"0":
        raise SystemExit("unmerged Git index entries are not deployable")
    if mode == b"160000":
        raise SystemExit(f"Git submodules are not supported: {raw_path!r}")
    if mode == b"120000":
        raise SystemExit(f"tracked symlinks are not supported in production releases: {raw_path!r}")
    if mode not in {b"100644", b"100755"}:
        raise SystemExit(f"unsupported Git entry mode {mode.decode()}: {raw_path!r}")
PY

source_manifest="$git_temp/source-manifest"
if [[ -z "$source_suffix" ]]; then
    git_as_checkout_owner ls-tree --name-only -r -z "$commit" > "$source_manifest"
else
    raw_manifest="$git_temp/source-manifest.raw"
    git_as_checkout_owner ls-files -z --cached --others --exclude-standard > "$raw_manifest"
    "$MUSIC_AGENT_PYTHON" - "$REPO_DIR" "$raw_manifest" "$source_manifest" <<'PY'
import os
import stat
import sys
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve(strict=True)
raw = Path(sys.argv[2]).read_bytes().split(b"\0")
output = Path(sys.argv[3])
accepted: list[bytes] = []
for encoded in raw:
    if not encoded:
        continue
    path_text = os.fsdecode(encoded)
    relative = PurePosixPath(path_text)
    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise SystemExit(f"unsafe Git path: {encoded!r}")
    source = root.joinpath(*relative.parts)
    try:
        metadata = source.lstat()
    except FileNotFoundError:
        # A deleted tracked path is intentionally absent from a dirty release.
        continue
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"release entries must be regular files: {path_text}")
    accepted.append(encoded)
output.write_bytes(b"\0".join(accepted) + (b"\0" if accepted else b""))
PY
fi

"$MUSIC_AGENT_PYTHON" - "$source_manifest" <<'PY'
import os
import sys
from pathlib import Path, PurePosixPath

manifest = Path(sys.argv[1]).read_bytes().split(b"\0")
seen: set[bytes] = set()
for encoded in manifest:
    if not encoded:
        continue
    if encoded in seen:
        raise SystemExit(f"duplicate release path: {encoded!r}")
    seen.add(encoded)
    value = os.fsdecode(encoded)
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SystemExit(f"unsafe release path: {encoded!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SystemExit(f"control character in release path: {encoded!r}")
    folded_parts = tuple(part.casefold() for part in path.parts)
    name = folded_parts[-1]
    if ".git" in folded_parts:
        raise SystemExit(f"Git metadata cannot enter a release: {value}")
    if "credentials" in folded_parts or "secrets" in folded_parts:
        raise SystemExit(f"credential/secret paths cannot enter a release: {value}")
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        raise SystemExit(f"environment override cannot enter a release: {value}")
    if name.endswith((".pem", ".key")):
        raise SystemExit(f"private-key-like path cannot enter a release: {value}")
PY
source_manifest_digest="$(music_agent_sha256 "$source_manifest")"

if [[ -z "$source_suffix" ]]; then
    source_archive="$git_temp/source.tar"
    git_as_checkout_owner archive --format=tar "$commit" > "$source_archive"
    tar --extract --file "$source_archive" --directory "$release" \
        --no-same-owner --no-same-permissions
else
    source_content_before="$git_temp/source-content-before"
    source_state_before="$git_temp/source-state-before"
    "$MUSIC_AGENT_PYTHON" "$SOURCE_SNAPSHOT" \
        "$REPO_DIR" "$source_manifest" "$source_content_before" "$source_state_before"
    rsync --archive --from0 --files-from="$source_manifest" --safe-links \
        --no-owner --no-group --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
        "$REPO_DIR/" "$release/"
    release_content="$git_temp/release-content"
    "$MUSIC_AGENT_PYTHON" "$SOURCE_SNAPSHOT" \
        "$release" "$source_manifest" "$release_content"
    cmp -s "$source_content_before" "$release_content" ||
        music_agent_die "the dirty release does not match its source content snapshot"
    status_after="$git_temp/status-after"
    git_as_checkout_owner status --porcelain=v1 -z --untracked-files=normal > "$status_after"
    cmp -s "$status_before" "$status_after" ||
        music_agent_die "the dirty checkout changed while its release snapshot was being built"
    source_content_after="$git_temp/source-content-after"
    source_state_after="$git_temp/source-state-after"
    "$MUSIC_AGENT_PYTHON" "$SOURCE_SNAPSHOT" \
        "$REPO_DIR" "$source_manifest" "$source_content_after" "$source_state_after"
    cmp -s "$source_content_before" "$source_content_after" ||
        music_agent_die "the dirty checkout content changed during release materialization"
    cmp -s "$source_state_before" "$source_state_after" ||
        music_agent_die "the dirty checkout metadata changed during release materialization"
fi
verify_original_git_index
cleanup_git_snapshot
git_temp=""
trap cleanup_incomplete_release EXIT

"$MUSIC_AGENT_PYTHON" -m venv "$release/venv"
lock_file="$release/requirements/production.lock"
[[ -f "$lock_file" ]] || music_agent_die "hashed requirements/production.lock is required"
"$release/venv/bin/python" -m pip install --disable-pip-version-check \
    --require-hashes --only-binary=:all: --requirement "$lock_file"
"$release/venv/bin/python" -m pip install --disable-pip-version-check \
    --no-build-isolation --no-deps "$release"
"$release/venv/bin/python" -m pip check
"$release/venv/bin/python" -m compileall -q "$release/app"
chmod 0755 "$release/scripts/"*.sh "$release/scripts/sqlite-maintenance.py"
schema_revision="$("$release/venv/bin/python" -c 'from app.db.engine import EXPECTED_SCHEMA_REVISION; print(EXPECTED_SCHEMA_REVISION)')"
music_agent_normalize_tool_tree

# The service account must be able to traverse and read the whole release, but
# may never write it. Normalizing here also repairs restrictive modes created by
# root's deployment umask before the target-user pre-activation probe runs.
chown -R root:"$MUSIC_AGENT_SERVICE_GROUP" "$release"
chmod -R u=rwX,g=rX,o= "$release"

previous_release="$(music_agent_current_release)"
previous_web_active=0 previous_worker_active=0
if music_agent_unit_exists music-agent-web.service && music_agent_systemctl is-active --quiet music-agent-web.service; then
    previous_web_active=1
fi
if music_agent_unit_exists music-agent-worker.service && music_agent_systemctl is-active --quiet music-agent-worker.service; then
    previous_worker_active=1
fi

"$release/scripts/validate.sh" --release "$release" --pre-activate --require-config-check

database_existed=0 predeploy_backup=""
write_manifest() {
    local status="${1:?status required}" output="$release/RELEASE.json"
    "$MUSIC_AGENT_PYTHON" - "$output" "$release_id" "$commit" "$source_suffix" \
        "$schema_revision" "$status" "$previous_release" "$predeploy_backup" \
        "$source_manifest_digest" "$repo_owner" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

(
    output,
    release_id,
    commit,
    source_suffix,
    schema,
    status,
    previous,
    backup,
    source_manifest_sha256,
    checkout_owner,
) = sys.argv[1:]
payload = {
    "format": 1,
    "release_id": release_id,
    "git_commit": commit,
    "dirty": bool(source_suffix),
    "source_manifest_sha256": source_manifest_sha256,
    "checkout_owner": checkout_owner,
    "schema_revision": schema,
    "status": status,
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "previous_release": previous or None,
    "predeploy_backup": backup or None,
}
Path(output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$output"
    chmod 0640 "$output"
}

# From this point the candidate has passed all build-time checks. Retain it as
# an explicitly prepared, inactive release if a later operational step fails;
# only never-validated builds are removed by the EXIT trap.
write_manifest prepared
unlink "$release/.build-incomplete"
trap - EXIT

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

if ! music_agent_stop_services; then
    restore_previous_units || true
    [[ "$previous_web_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$previous_worker_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "could not stop both services; database was not touched"
fi
[[ -f "$MUSIC_AGENT_DB" ]] && database_existed=1
if [[ "$database_existed" -eq 1 ]]; then
    if ! predeploy_backup="$("$release/scripts/backup.sh" --protected \
            --label "predeploy-$release_id" --quiet)"; then
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
    music_agent_die "could not install the candidate systemd units; release was not activated"
fi

restore_previous_database() {
    if [[ "$database_existed" -eq 1 && -n "$predeploy_backup" ]]; then
        "$MUSIC_AGENT_PYTHON" "$release/scripts/sqlite-maintenance.py" restore \
            --require-checksum --source "$predeploy_backup" \
            --destination "$MUSIC_AGENT_DB" >/dev/null || return 1
        chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB" || return 1
        chmod 0640 "$MUSIC_AGENT_DB" || return 1
    elif [[ "$database_existed" -eq 0 && -f "$MUSIC_AGENT_DB" ]]; then
        failed_db="$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id-failed-new-database.db"
        mv "$MUSIC_AGENT_DB" "$failed_db" || return 1
        chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$failed_db" || return 1
        chmod 0640 "$failed_db" || return 1
        for suffix in -journal -wal -shm; do
            if [[ -e "$MUSIC_AGENT_DB$suffix" ]]; then
                mv "$MUSIC_AGENT_DB$suffix" "$failed_db$suffix" || return 1
                chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$failed_db$suffix" || return 1
                chmod 0640 "$failed_db$suffix" || return 1
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
        music_agent_atomic_symlink "$previous_release" "$MUSIC_AGENT_CURRENT_LINK" || recovery_ok=0
    elif [[ -L "$MUSIC_AGENT_CURRENT_LINK" ]]; then
        unlink "$MUSIC_AGENT_CURRENT_LINK" || recovery_ok=0
    fi
    restore_previous_database || recovery_ok=0
    restore_previous_units || recovery_ok=0
    chmod u+w "$release/RELEASE.json" || recovery_ok=0
    write_manifest failed || recovery_ok=0
    cp "$release/RELEASE.json" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json" || recovery_ok=0
    chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json" || recovery_ok=0
    chmod 0640 "$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json" || recovery_ok=0
    chown -R root:"$MUSIC_AGENT_SERVICE_GROUP" "$release" || recovery_ok=0
    chmod -R a-w "$release" || recovery_ok=0
    if [[ "$recovery_ok" -eq 1 ]]; then
        if [[ "$previous_web_active" -eq 1 ]]; then music_agent_systemctl start music-agent-web.service || true; fi
        if [[ "$previous_worker_active" -eq 1 ]]; then music_agent_systemctl start music-agent-worker.service || true; fi
    else
        music_agent_warn "automatic database/unit restoration failed; services remain stopped"
    fi
    music_agent_die "$reason"
}

if ! write_manifest prepared; then
    recover_activation "could not record the pre-deployment backup in the release manifest"
fi
if ! music_agent_with_credentials "$release/venv/bin/music-agent" migrate; then
    recover_activation "database migration failed"
fi
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    if ! chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB" ||
            ! chmod 0640 "$MUSIC_AGENT_DB"; then
        recover_activation "could not secure the migrated database"
    fi
fi
if ! music_agent_with_credentials "$release/venv/bin/music-agent" validate; then
    recover_activation "post-migration validation failed"
fi
if ! music_agent_atomic_symlink "$release" "$MUSIC_AGENT_CURRENT_LINK"; then
    recover_activation "could not activate the candidate release symlink"
fi
if ! "$release/scripts/validate.sh" --release "$release"; then
    recover_activation "activated release validation failed"
fi

finalize_release() {
    write_manifest active || return 1
    chown -R root:"$MUSIC_AGENT_SERVICE_GROUP" "$release" || return 1
    chmod -R a-w "$release"
}
if ! finalize_release; then
    recover_activation "could not make the activated release immutable"
fi

if ! "$release/scripts/validate.sh" --release "$release"; then
    recover_activation "post-activation validation failed"
fi

# No credential material is copied into a service-UID-readable runtime directory
# after this point. The web unit performs its own credential-backed ExecStartPre;
# once both units are active, the remaining deployment check is service state only.
if [[ "$start_services" -eq 1 ]]; then
    if ! music_agent_start_services ||
            ! music_agent_systemctl is-active --quiet music-agent-web.service ||
            ! music_agent_systemctl is-active --quiet music-agent-worker.service; then
        recover_activation "service start failed"
    fi
fi

record_successful_deployment() {
    local record="$MUSIC_AGENT_DEPLOYMENT_DIR/$release_id.json"
    cp "$release/RELEASE.json" "$record" || return 1
    chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$record" || return 1
    chmod 0640 "$record"
}
if ! record_successful_deployment; then
    recover_activation "could not record the successful deployment"
fi
music_agent_log "release $release_id activated; pre-deployment backup: ${predeploy_backup:-not needed}"
