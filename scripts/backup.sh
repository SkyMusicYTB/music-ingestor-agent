#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    printf 'Usage: scripts/backup.sh [--label LABEL] [--quiet]\n'
}

label="manual" quiet=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --label) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; label="$2"; shift 2 ;;
        --quiet) quiet=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ "$label" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || music_agent_die "invalid backup label"
[[ -f "$MUSIC_AGENT_DB" ]] || music_agent_die "database does not exist: $MUSIC_AGENT_DB"
[[ "$EUID" -eq 0 || "$(id -un)" == "$MUSIC_AGENT_SERVICE_USER" ]] ||
    music_agent_die "run backup as root or $MUSIC_AGENT_SERVICE_USER"

install -d -m 0750 "$MUSIC_AGENT_BACKUP_DIR"
exec 8>"$MUSIC_AGENT_BACKUP_DIR/.backup.lock"
flock -n 8 || music_agent_die "another backup is running"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="$MUSIC_AGENT_BACKUP_DIR/$timestamp-$label.db"
counter=0
while [[ -e "$destination" ]]; do
    counter=$((counter + 1))
    destination="$MUSIC_AGENT_BACKUP_DIR/$timestamp-$label-$counter.db"
done

"$MUSIC_AGENT_PYTHON" "$SCRIPT_DIR/sqlite-maintenance.py" backup \
    --source "$MUSIC_AGENT_DB" --destination "$destination" --label "$label" >/dev/null
if [[ "$EUID" -eq 0 ]]; then
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" \
        "$destination" "$destination.sha256" "$destination.json"
fi
chmod 0600 "$destination" "$destination.sha256" "$destination.json"
[[ "$quiet" -eq 1 ]] || music_agent_log "verified SQLite backup: $destination"
printf '%s\n' "$destination"
