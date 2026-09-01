#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: sudo scripts/validate.sh [--release PATH] [--pre-activate] [--services]

--pre-activate validates a staged release without querying the production DB.
--services additionally requires the enabled web and worker units to be active.
EOF
}

release="" pre_activate=0 require_services=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --release) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; release="$2"; shift 2 ;;
        --pre-activate) pre_activate=1; shift ;;
        --services) require_services=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done

music_agent_require_root
music_agent_assert_supported_host
music_agent_require_command flock

if [[ -z "$release" ]]; then
    release="$(music_agent_current_release)"
fi
[[ -n "$release" && -d "$release" ]] || music_agent_die "no installed release found"
release="$(cd -- "$release" && pwd -P)"
music_agent_assert_within "$release" "$MUSIC_AGENT_RELEASES_DIR"
[[ -x "$release/venv/bin/python" ]] || music_agent_die "release virtualenv is missing"
[[ -x "$release/venv/bin/music-agent" ]] || music_agent_die "music-agent entry point is missing"
"$release/venv/bin/python" -c 'import app, sqlalchemy, fastapi' >/dev/null
"$release/venv/bin/python" -m pip check >/dev/null

[[ -r "$MUSIC_AGENT_ENV_FILE" ]] || music_agent_die "configuration is missing: $MUSIC_AGENT_ENV_FILE"
music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
env -i "PATH=/usr/bin:/bin" "${MUSIC_AGENT_CONFIG_ENV[@]}" \
    "$release/scripts/validate-runtime-environment.sh"
if grep -Eq '^[[:space:]]*MUSIC_AGENT_(AUTH_HMAC_KEY|OPENAI_API_KEY|LISTENBRAINZ_TOKEN)=' "$MUSIC_AGENT_ENV_FILE"; then
    music_agent_die "secrets must not be stored in $MUSIC_AGENT_ENV_FILE; use scripts/set-secret.sh"
fi
config_mode="$(stat -c '%a' "$MUSIC_AGENT_ENV_FILE")"
config_owner="$(stat -c '%U:%G' "$MUSIC_AGENT_ENV_FILE")"
[[ "$config_owner" == "root:$MUSIC_AGENT_SERVICE_GROUP" && "$config_mode" == "640" ]] ||
    music_agent_die "configuration must be root:$MUSIC_AGENT_SERVICE_GROUP mode 0640"

credential=""
for credential in auth_hmac_key openai_api_key listenbrainz_token; do
    credential_path="$MUSIC_AGENT_CREDENTIAL_DIR/$credential"
    [[ -f "$credential_path" ]] || music_agent_die "credential file is missing: $credential_path"
    [[ "$(stat -c '%U:%G:%a' "$credential_path")" == "root:root:600" ]] ||
        music_agent_die "$credential_path must be root:root mode 0600"
done
[[ "$(wc -c < "$MUSIC_AGENT_CREDENTIAL_DIR/auth_hmac_key")" -ge 33 ]] ||
    music_agent_die "auth_hmac_key is too short"

for executable in yt-dlp deno; do
    link="$MUSIC_AGENT_TOOL_BIN/$executable"
    [[ -L "$link" ]] || music_agent_die "tool link is missing: $link"
    target="$(readlink -f "$link")"
    music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/$executable"
    [[ -x "$target" ]] || music_agent_die "tool is not executable: $target"
    provenance="$(dirname "$(dirname "$target")")/artifact.sha256"
    [[ -r "$provenance" && "$(<"$provenance")" =~ ^[0-9a-f]{64}$ ]] ||
        music_agent_die "tool provenance is missing or invalid: $provenance"
    version_directory="$(dirname "$(dirname "$target")")"
    binary_provenance="$version_directory/binary.sha256"
    [[ -r "$binary_provenance" && "$(<"$binary_provenance")" =~ ^[0-9a-f]{64}$ ]] ||
        music_agent_die "installed tool digest is missing or invalid: $binary_provenance"
    music_agent_verify_sha256 "$target" "$(<"$binary_provenance")"
    [[ "$(stat -c '%U:%G' "$version_directory")" == "root:root" ]] ||
        music_agent_die "tool directory must be root-owned: $version_directory"
    [[ -z "$(find "$version_directory" -xdev -perm /022 -print -quit)" ]] ||
        music_agent_die "tool directory contains group/world-writable content: $version_directory"
done
yt_version="$(PATH=/usr/bin:/bin "$MUSIC_AGENT_TOOL_BIN/yt-dlp" --version)"
deno_version="$("$MUSIC_AGENT_TOOL_BIN/deno" --version | awk 'NR == 1 {print $2}')"
[[ "$(readlink -f "$MUSIC_AGENT_TOOL_BIN/yt-dlp")" == *"/$yt_version/bin/yt-dlp" ]] ||
    music_agent_die "yt-dlp version does not match its immutable directory"
[[ "$(readlink -f "$MUSIC_AGENT_TOOL_BIN/deno")" == *"/$deno_version/bin/deno" ]] ||
    music_agent_die "Deno version does not match its immutable directory"

for directory in "$MUSIC_AGENT_STATE_DIR" "$MUSIC_AGENT_BACKUP_DIR" \
        "$MUSIC_AGENT_DOWNLOAD_DIR" "$MUSIC_AGENT_MUSIC_DIR"; do
    [[ -d "$directory" ]] || music_agent_die "required directory is missing: $directory"
done

if [[ "$pre_activate" -eq 0 ]]; then
    [[ "$(music_agent_current_release)" == "$release" ]] || music_agent_die "release is not the active symlink target"
    if [[ -f "$release/RELEASE.json" ]] && \
            [[ "$("$MUSIC_AGENT_PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$release/RELEASE.json")" == "active" ]]; then
        [[ "$(stat -c '%U:%G' "$release")" == "root:root" ]] ||
            music_agent_die "active release must be root-owned"
        [[ -z "$(find "$release" -xdev -perm /022 -print -quit)" ]] ||
            music_agent_die "active release contains writable content"
    fi
    if [[ -f "$MUSIC_AGENT_DB" ]]; then
        "$MUSIC_AGENT_PYTHON" "$release/scripts/sqlite-maintenance.py" verify "$MUSIC_AGENT_DB" >/dev/null
        [[ "$(stat -c '%U:%G:%a' "$MUSIC_AGENT_DB")" == \
            "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP:640" ]] ||
            music_agent_die "database must be $MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP mode 0640"
    fi
    music_agent_with_credentials "$release/venv/bin/music-agent" validate

    if command -v systemd-analyze >/dev/null 2>&1 && [[ "${MUSIC_AGENT_TEST_MODE:-0}" != "1" ]]; then
        systemd-analyze verify \
            "$MUSIC_AGENT_UNIT_DIR/music-agent-web.service" \
            "$MUSIC_AGENT_UNIT_DIR/music-agent-worker.service" \
            "$MUSIC_AGENT_UNIT_DIR/music-agent-backup.service" \
            "$MUSIC_AGENT_UNIT_DIR/music-agent-backup.timer" >/dev/null
        systemd-analyze security --offline=yes music-agent-web.service >/dev/null || true
        systemd-analyze security --offline=yes music-agent-worker.service >/dev/null || true
    fi
fi

if [[ "$require_services" -eq 1 ]]; then
    music_agent_systemctl is-active --quiet music-agent-web.service
    music_agent_systemctl is-active --quiet music-agent-worker.service
fi
music_agent_log "validation passed for $release (yt-dlp $yt_version, Deno $deno_version)"
