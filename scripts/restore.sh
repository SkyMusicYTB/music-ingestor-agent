#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    printf 'Usage: sudo scripts/restore.sh --backup /var/lib/music-agent/backups/FILE.db\n'
}

backup_file=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --backup) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; backup_file="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ -n "$backup_file" ]] || { usage >&2; exit 64; }

music_agent_require_root
music_agent_assert_supported_host
music_agent_acquire_lock operations
backup_file="$(readlink -f "$backup_file")"
music_agent_assert_within "$backup_file" "$MUSIC_AGENT_BACKUP_DIR"
"$MUSIC_AGENT_PYTHON" "$SCRIPT_DIR/sqlite-maintenance.py" verify \
    --require-checksum "$backup_file" >/dev/null

current_release="$(music_agent_current_release)"
[[ -n "$current_release" ]] || music_agent_die "no active release"
web_was_active=0 worker_was_active=0
if music_agent_systemctl is-active --quiet music-agent-web.service; then
    web_was_active=1
fi
if music_agent_systemctl is-active --quiet music-agent-worker.service; then
    worker_was_active=1
fi
if ! music_agent_stop_services; then
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "could not stop both services; restore was not attempted"
fi

safety_backup="" database_existed=0
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    database_existed=1
    if ! safety_backup="$("$SCRIPT_DIR/backup.sh" --label pre-restore --quiet)"; then
        [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
        [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
        music_agent_die "pre-restore safety backup failed; restore was not attempted"
    fi
fi
if [[ -e "$MUSIC_AGENT_DB-journal" || -e "$MUSIC_AGENT_DB-wal" || -e "$MUSIC_AGENT_DB-shm" ]]; then
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "SQLite sidecar remains after service stop; refusing restore"
fi

restore_failed=0
"$MUSIC_AGENT_PYTHON" "$SCRIPT_DIR/sqlite-maintenance.py" restore \
    --require-checksum --source "$backup_file" --destination "$MUSIC_AGENT_DB" >/dev/null || restore_failed=1
if [[ -f "$MUSIC_AGENT_DB" ]]; then
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
    chmod 0640 "$MUSIC_AGENT_DB"
fi
if [[ "$restore_failed" -eq 0 ]]; then
    music_agent_with_credentials "$current_release/venv/bin/music-agent" migrate || restore_failed=1
    music_agent_with_credentials "$current_release/venv/bin/music-agent" validate || restore_failed=1
fi
if [[ "$restore_failed" -eq 0 ]]; then
    if ! music_agent_start_services; then
        restore_failed=1
    elif ! "$current_release/scripts/validate.sh" --release "$current_release" --services; then
        restore_failed=1
    fi
fi
if [[ "$restore_failed" -ne 0 ]]; then
    if ! music_agent_stop_services; then
        music_agent_die "database restore failed and services could not be stopped for automatic recovery"
    fi
    if [[ -e "$MUSIC_AGENT_DB-journal" || -e "$MUSIC_AGENT_DB-wal" || -e "$MUSIC_AGENT_DB-shm" ]]; then
        music_agent_die "database restore failed and a SQLite sidecar blocks automatic recovery"
    elif [[ "$database_existed" -eq 1 && -n "$safety_backup" ]]; then
        music_agent_warn "restore failed; reinstating the pre-restore database"
        "$MUSIC_AGENT_PYTHON" "$SCRIPT_DIR/sqlite-maintenance.py" restore \
            --require-checksum --source "$safety_backup" --destination "$MUSIC_AGENT_DB" >/dev/null
        chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DB"
        chmod 0640 "$MUSIC_AGENT_DB"
    elif [[ "$database_existed" -eq 0 && -f "$MUSIC_AGENT_DB" ]]; then
        failed_database="$MUSIC_AGENT_DEPLOYMENT_DIR/restore-failed-$(date -u +%Y%m%dT%H%M%SZ).db"
        mv "$MUSIC_AGENT_DB" "$failed_database"
        chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$failed_database"
        chmod 0640 "$failed_database"
    fi
    [[ "$web_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-web.service || true
    [[ "$worker_was_active" -eq 0 ]] || music_agent_systemctl start music-agent-worker.service || true
    music_agent_die "database restore failed"
fi
music_agent_log "restored and validated $backup_file (safety backup: ${safety_backup:-none})"
