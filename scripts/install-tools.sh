#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=scripts/lib/tooling.sh
source "$SCRIPT_DIR/lib/tooling.sh"

music_agent_require_root
music_agent_assert_supported_host
music_agent_acquire_lock operations
music_agent_install_pinned_tools "$SCRIPT_DIR/../requirements/tool-pins.env"
music_agent_log "installed pinned Deno $DENO_VERSION and yt-dlp $YT_DLP_VERSION"
