#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=scripts/lib/tooling.sh
source "$SCRIPT_DIR/lib/tooling.sh"

usage() {
    cat <<'EOF'
Usage:
  sudo scripts/update-yt-dlp.sh
  sudo scripts/update-yt-dlp.sh --version YYYY.MM.DD --sha256 HEX

With no arguments, installs the repository-audited pin. An out-of-band update must
provide both the official release version and its published SHA-256 digest.
EOF
}

requested_version="" requested_digest=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; requested_version="$2"; shift 2 ;;
        --sha256) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; requested_digest="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done

music_agent_require_root
music_agent_assert_supported_host
music_agent_require_command runuser
music_agent_acquire_lock operations
music_agent_normalize_tool_tree

if [[ -z "$requested_version" && -z "$requested_digest" ]]; then
    music_agent_read_tool_pins "$SCRIPT_DIR/../requirements/tool-pins.env"
    requested_version="$YT_DLP_VERSION"
    requested_digest="$YT_DLP_SHA256"
elif [[ -z "$requested_version" || -z "$requested_digest" ]]; then
    music_agent_die "--version and --sha256 must be supplied together"
fi
[[ "$requested_version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]] || music_agent_die "invalid yt-dlp version"
[[ "$requested_digest" =~ ^[0-9a-f]{64}$ ]] || music_agent_die "invalid SHA-256 digest"

readonly url="https://github.com/yt-dlp/yt-dlp/releases/download/$requested_version/yt-dlp"
old_target=""
if [[ -L "$MUSIC_AGENT_TOOL_BIN/yt-dlp" ]]; then
    old_target="$(readlink "$MUSIC_AGENT_TOOL_BIN/yt-dlp")"
fi

music_agent_install_yt_dlp "$requested_version" "$url" "$requested_digest"
music_agent_normalize_tool_tree
if ! music_agent_probe_tools_as_service >/dev/null; then
    if [[ -n "$old_target" ]]; then
        music_agent_warn "runtime-user tool validation failed; restoring the previous yt-dlp link"
        music_agent_atomic_symlink "$old_target" "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
        chown -h root:root "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
        music_agent_normalize_tool_tree
    else
        music_agent_warn "runtime-user tool validation failed; removing the unvalidated yt-dlp link"
        unlink "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
    fi
    music_agent_die "yt-dlp update is not executable by the service account"
fi
if music_agent_unit_exists music-agent-worker.service && \
        music_agent_systemctl is-active --quiet music-agent-worker.service; then
    if ! music_agent_systemctl restart music-agent-worker.service; then
        if [[ -n "$old_target" ]]; then
            music_agent_warn "worker restart failed; restoring the previous yt-dlp link"
            music_agent_atomic_symlink "$old_target" "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
            chown -h root:root "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
            music_agent_normalize_tool_tree
            music_agent_systemctl restart music-agent-worker.service || true
        else
            unlink "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
        fi
        music_agent_die "yt-dlp update could not be activated"
    fi
fi

install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR"
printf '%s yt-dlp=%s sha256=%s\n' "$(date -u +%FT%TZ)" "$requested_version" "$requested_digest" \
    >> "$MUSIC_AGENT_DEPLOYMENT_DIR/tool-updates.log"
chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR/tool-updates.log"
chmod 0640 "$MUSIC_AGENT_DEPLOYMENT_DIR/tool-updates.log"
music_agent_log "yt-dlp $requested_version is active; Deno was not changed"
