#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage:
  sudo scripts/music-agentctl.sh validate
  sudo scripts/music-agentctl.sh migrate
  sudo scripts/music-agentctl.sh scan [--full]
  sudo scripts/music-agentctl.sh admin-reset

Runs an allowlisted administrative CLI command as the service account with
safely parsed production configuration and no web-service credentials.
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 64; }
subcommand="$1"; shift
case "$subcommand" in
    validate|migrate|admin-reset)
        [[ $# -eq 0 ]] || { usage >&2; exit 64; }
        arguments=("$subcommand")
        ;;
    scan)
        if [[ $# -eq 0 ]]; then
            arguments=(scan)
        elif [[ $# -eq 1 && "$1" == "--full" ]]; then
            arguments=(scan --full)
        else
            usage >&2
            exit 64
        fi
        ;;
    *) usage >&2; exit 64 ;;
esac

music_agent_require_root
music_agent_acquire_lock operations
current_release="$(music_agent_current_release)"
[[ -n "$current_release" && -x "$current_release/venv/bin/music-agent" ]] ||
    music_agent_die "no active Music Agent release"
music_agent_without_credentials "$current_release/venv/bin/music-agent" "${arguments[@]}"
