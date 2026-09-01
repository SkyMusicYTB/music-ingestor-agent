#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: sudo scripts/rollback.sh [RELEASE_ID] [--restore-backup FILE.db]

Without RELEASE_ID, uses the active manifest's previous_release. A database
backup is mandatory when the target release expects a different schema.
EOF
}

target_id="" restore_backup=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --restore-backup) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; restore_backup="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; exit 64 ;;
        *) [[ -z "$target_id" ]] || { usage >&2; exit 64; }; target_id="$1"; shift ;;
    esac
done

music_agent_require_root
music_agent_assert_supported_host
music_agent_acquire_lock operations
current_release="$(music_agent_current_release)"
[[ -n "$current_release" && -f "$current_release/RELEASE.json" ]] || music_agent_die "active release manifest is missing"

json_field() {
    "$MUSIC_AGENT_PYTHON" - "${1:?JSON file required}" "${2:?field required}" <<'PY'
import json
import sys
value = json.loads(open(sys.argv[1], encoding="utf-8").read()).get(sys.argv[2])
if value is not None:
    print(value)
PY
}

if [[ -z "$target_id" ]]; then
    previous_path="$(json_field "$current_release/RELEASE.json" previous_release)"
    [[ -n "$previous_path" ]] || music_agent_die "manifest does not identify a previous release"
    target_id="${previous_path##*/}"
fi
[[ "$target_id" =~ ^[0-9a-f]{12}(-dirty)?-[0-9]{8}T[0-9]{6}Z$ ]] || music_agent_die "invalid release identifier"
target_release="$MUSIC_AGENT_RELEASES_DIR/$target_id"
music_agent_assert_within "$target_release" "$MUSIC_AGENT_RELEASES_DIR"
[[ -d "$target_release" && -f "$target_release/RELEASE.json" ]] || music_agent_die "release not found: $target_id"
[[ "$target_release" != "$current_release" ]] || music_agent_die "target release is already active"
target_status="$(json_field "$target_release/RELEASE.json" status)"
[[ "$target_status" == "active" ]] || music_agent_die "refusing rollback to release with status $target_status"
target_schema="$(json_field "$target_release/RELEASE.json" schema_revision)"
[[ -n "$target_schema" ]] || music_agent_die "target schema metadata is missing"

database_schema=""
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    database_schema="$("$MUSIC_AGENT_PYTHON" - "$MUSIC_AGENT_DB" <<'PY'
import sqlite3
import sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
try:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    if exists:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        if row:
            print(row[0])
finally:
    connection.close()
PY
)"
fi

if [[ -n "$restore_backup" ]]; then
    restore_backup="$(readlink -f "$restore_backup")"
    music_agent_assert_within "$restore_backup" "$MUSIC_AGENT_BACKUP_DIR"
    "$MUSIC_AGENT_PYTHON" "$target_release/scripts/sqlite-maintenance.py" verify \
        --require-checksum "$restore_backup" >/dev/null
    backup_schema="$("$MUSIC_AGENT_PYTHON" - "$restore_backup.json" <<'PY'
import json
import sys
print(json.loads(open(sys.argv[1], encoding="utf-8").read()).get("schema_revision") or "")
PY
)"
    [[ "$backup_schema" == "$target_schema" ]] || music_agent_die "backup schema $backup_schema does not match target $target_schema"
elif [[ "$database_schema" != "$target_schema" ]]; then
    music_agent_die "database schema ${database_schema:-none} differs from target $target_schema; supply --restore-backup"
fi

web_was_active=0 worker_was_active=0
music_agent_systemctl is-active --quiet music-agent-web.service && web_was_active=1 || true
music_agent_systemctl is-active --quiet music-agent-worker.service && worker_was_active=1 || true
if ! music_agent_stop_services; then
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "could not stop both services; rollback was not attempted"
fi
safety_backup=""
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    if ! safety_backup="$("$current_release/scripts/backup.sh" --label "prerollback-$target_id" --quiet)"; then
        [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
        [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
        music_agent_die "pre-rollback safety backup failed; rollback was not attempted"
    fi
fi
if [[ -e "$MUSIC_AGENT_DB-journal" || -e "$MUSIC_AGENT_DB-wal" || -e "$MUSIC_AGENT_DB-shm" ]]; then
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "SQLite sidecar remains after service stop; rollback was not attempted"
fi

rollback_failed=0
if [[ -n "$restore_backup" ]]; then
    "$MUSIC_AGENT_PYTHON" "$target_release/scripts/sqlite-maintenance.py" restore \
        --require-checksum --source "$restore_backup" --destination "$MUSIC_AGENT_DB" >/dev/null || rollback_failed=1
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
    chmod 0640 "$MUSIC_AGENT_DB"
fi
if [[ "$rollback_failed" -eq 0 ]]; then
    music_agent_atomic_symlink "$target_release" "$MUSIC_AGENT_CURRENT_LINK"
    for unit in music-agent-web.service music-agent-worker.service music-agent-backup.service music-agent-backup.timer; do
        install -m 0644 -o root -g root "$target_release/systemd/$unit" "$MUSIC_AGENT_UNIT_DIR/$unit"
    done
    music_agent_systemctl daemon-reload
    music_agent_with_credentials "$target_release/venv/bin/music-agent" validate || rollback_failed=1
    if [[ "$rollback_failed" -eq 0 ]]; then
        if ! music_agent_start_services; then
            rollback_failed=1
        elif ! "$target_release/scripts/validate.sh" --release "$target_release" --services; then
            rollback_failed=1
        fi
    fi
fi

if [[ "$rollback_failed" -ne 0 ]]; then
    music_agent_warn "rollback activation failed; restoring the code/database from before rollback"
    if ! music_agent_stop_services; then
        music_agent_die "rollback failed and automatic recovery could not stop all services"
    fi
    music_agent_atomic_symlink "$current_release" "$MUSIC_AGENT_CURRENT_LINK"
    for unit in music-agent-web.service music-agent-worker.service music-agent-backup.service music-agent-backup.timer; do
        install -m 0644 -o root -g root "$current_release/systemd/$unit" "$MUSIC_AGENT_UNIT_DIR/$unit"
    done
    music_agent_systemctl daemon-reload
    if [[ -n "$safety_backup" ]]; then
        if [[ -e "$MUSIC_AGENT_DB-journal" || -e "$MUSIC_AGENT_DB-wal" || -e "$MUSIC_AGENT_DB-shm" ]]; then
            music_agent_die "rollback failed and a SQLite sidecar blocks automatic database recovery"
        fi
        "$MUSIC_AGENT_PYTHON" "$current_release/scripts/sqlite-maintenance.py" restore \
            --require-checksum --source "$safety_backup" --destination "$MUSIC_AGENT_DB" >/dev/null
        chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
        chmod 0640 "$MUSIC_AGENT_DB"
    fi
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "rollback failed; previous release was reinstated"
fi

record="$MUSIC_AGENT_DEPLOYMENT_DIR/rollback-$(date -u +%Y%m%dT%H%M%SZ)-to-$target_id.json"
"$MUSIC_AGENT_PYTHON" - "$record" "$current_release" "$target_release" "$safety_backup" "$restore_backup" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
payload = {
    "format": 1,
    "kind": "rollback",
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "from": sys.argv[2],
    "to": sys.argv[3],
    "safety_backup": sys.argv[4] or None,
    "restored_backup": sys.argv[5] or None,
}
Path(sys.argv[1]).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$record"
chmod 0640 "$record"
music_agent_log "rolled back to $target_id; safety backup: ${safety_backup:-none}"
