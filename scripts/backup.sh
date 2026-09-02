#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    printf 'Usage: scripts/backup.sh [--label LABEL] [--protected] [--quiet]\n'
}

label="manual" protected=0 quiet=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --label) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; label="$2"; shift 2 ;;
        --protected) protected=1; shift ;;
        --quiet) quiet=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ "$label" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || music_agent_die "invalid backup label"
[[ -f "$MUSIC_AGENT_DB" ]] || music_agent_die "database does not exist: $MUSIC_AGENT_DB"
[[ "$EUID" -eq 0 || "$(id -un)" == "$MUSIC_AGENT_SERVICE_USER" ]] ||
    music_agent_die "run backup as root or $MUSIC_AGENT_SERVICE_USER"
[[ "$protected" -eq 0 || "$EUID" -eq 0 || "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]] ||
    music_agent_die "protected transaction backups require root"
music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
music_agent_assert_managed_production_config

destination_dir="$MUSIC_AGENT_BACKUP_DIR"
if [[ "$protected" -eq 1 ]]; then
    music_agent_prepare_transaction_backup_dir
    destination_dir="$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"
else
    [[ ! -L "$MUSIC_AGENT_BACKUP_DIR" ]] ||
        music_agent_die "backup directory must not be a symlink"
    install -d -m 0750 "$MUSIC_AGENT_BACKUP_DIR"
fi
# The state directory has a root-controlled parent and is shared by ordinary
# and protected backups. Locking its open directory inode preserves one lock
# domain without opening a service-controlled pathname for output as root.
music_agent_acquire_directory_lock "$MUSIC_AGENT_STATE_DIR" "backup"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$destination_dir/$timestamp-$label.db"
counter=0
while [[ -e "$destination" ]]; do
    counter=$((counter + 1))
    destination="$destination_dir/$timestamp-$label-$counter.db"
done

"$MUSIC_AGENT_PYTHON" "$SCRIPT_DIR/sqlite-maintenance.py" backup \
    --source "$MUSIC_AGENT_DB" --destination "$destination" --label "$label" >/dev/null
if [[ "$protected" -eq 1 && "$EUID" -eq 0 ]]; then
    chown root:root "$destination" "$destination.sha256" "$destination.json"
elif [[ "$EUID" -eq 0 ]]; then
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" \
        "$destination" "$destination.sha256" "$destination.json"
fi
chmod 0600 "$destination" "$destination.sha256" "$destination.json"
[[ "$quiet" -eq 1 ]] || music_agent_log "verified SQLite backup: $destination"
printf '%s\n' "$destination"
