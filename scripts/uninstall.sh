#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: sudo scripts/uninstall.sh --yes [--keep-code]

Removes managed systemd units and, unless --keep-code is used, /opt/music-agent.
Always preserves music, downloads, database/state/backups, configuration,
credentials, library ACLs, and the service account.
EOF
}

confirmed=0 keep_code=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes) confirmed=1; shift ;;
        --keep-code) keep_code=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ "$confirmed" -eq 1 ]] || music_agent_die "refusing uninstall without --yes"
music_agent_require_root
[[ "$(uname -s)" == "Linux" || "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]] || music_agent_die "uninstall requires Linux"
music_agent_acquire_lock operations

for unit in music-agent-backup.timer music-agent-worker.service music-agent-web.service; do
    music_agent_systemctl disable --now "$unit" >/dev/null 2>&1 || true
done
music_agent_systemctl stop music-agent-backup.service >/dev/null 2>&1 || true
for unit in music-agent-web.service music-agent-worker.service music-agent-backup.service music-agent-backup.timer; do
    unit_path="$MUSIC_AGENT_UNIT_DIR/$unit"
    [[ "$unit_path" == "$(music_agent_path /etc/systemd/system/)"* ]] || music_agent_die "unsafe unit path"
    rm -f "$unit_path"
done
music_agent_systemctl daemon-reload
music_agent_systemctl reset-failed >/dev/null 2>&1 || true

if [[ "$keep_code" -eq 0 && -d "$MUSIC_AGENT_OPT_DIR" ]]; then
    [[ "$MUSIC_AGENT_OPT_DIR" == "$(music_agent_path /opt/music-agent)" ]] || music_agent_die "unsafe code path"
    find "$MUSIC_AGENT_OPT_DIR" -depth -delete
fi

if [[ "$keep_code" -eq 1 ]]; then
    music_agent_log "uninstalled managed units and preserved code"
else
    music_agent_log "uninstalled managed units and application code"
fi
music_agent_log "preserved all data, configuration, credentials, ACLs, and accounts"
printf '%s\n' \
    "Preserved: $MUSIC_AGENT_MUSIC_DIR" \
    "Preserved: $MUSIC_AGENT_DOWNLOAD_DIR" \
    "Preserved: $MUSIC_AGENT_STATE_DIR" \
    "Preserved: $MUSIC_AGENT_ETC_DIR"
