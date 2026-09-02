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
Usage: sudo scripts/install.sh [--navidrome-unit UNIT | --navidrome-user USER]
                               [--allow-dirty] [--no-start]
EOF
}

navidrome_args=() deploy_args=(--from-install)
start_services=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --navidrome-unit|--navidrome-user)
            [[ $# -ge 2 ]] || { usage >&2; exit 64; }
            navidrome_args+=("$1" "$2"); shift 2 ;;
        --allow-dirty) deploy_args+=(--allow-dirty); shift ;;
        --no-start) deploy_args+=(--no-start); start_services=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done

music_agent_require_root
music_agent_assert_supported_host
music_agent_acquire_lock operations

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends \
    acl ca-certificates curl ffmpeg git openssl python3 python3-venv rsync unzip

if ! getent group "$MUSIC_AGENT_SERVICE_GROUP" >/dev/null; then
    groupadd --system "$MUSIC_AGENT_SERVICE_GROUP"
fi
if ! id "$MUSIC_AGENT_SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$MUSIC_AGENT_SERVICE_GROUP" \
        --home-dir "$MUSIC_AGENT_STATE_DIR" --shell /usr/sbin/nologin \
        --no-create-home "$MUSIC_AGENT_SERVICE_USER"
fi
[[ "$(id -u "$MUSIC_AGENT_SERVICE_USER")" -ne 0 ]] || music_agent_die "service account must not be root"
[[ "$(id -gn "$MUSIC_AGENT_SERVICE_USER")" == "$MUSIC_AGENT_SERVICE_GROUP" ]] ||
    music_agent_die "existing service account has an unexpected primary group"
service_shell="$(getent passwd "$MUSIC_AGENT_SERVICE_USER" | cut -d: -f7)"
[[ "$service_shell" == "/usr/sbin/nologin" || "$service_shell" == "/bin/false" ]] ||
    music_agent_die "existing service account must use a non-login shell"

install -d -m 0755 -o root -g root "$MUSIC_AGENT_OPT_DIR" "$MUSIC_AGENT_TOOLS_DIR"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_RELEASES_DIR"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_ETC_DIR"
install -d -m 0700 -o root -g root "$MUSIC_AGENT_CREDENTIAL_DIR"
install -d -m 0750 -o "$MUSIC_AGENT_SERVICE_USER" -g "$MUSIC_AGENT_SERVICE_GROUP" \
    "$MUSIC_AGENT_STATE_DIR" "$MUSIC_AGENT_STATE_DIR/artwork" "$MUSIC_AGENT_BACKUP_DIR"
install -d -m 0750 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_DEPLOYMENT_DIR"
music_agent_prepare_transaction_backup_dir
install -d -m 0750 -o "$MUSIC_AGENT_SERVICE_USER" -g "$MUSIC_AGENT_SERVICE_GROUP" \
    "$MUSIC_AGENT_DOWNLOAD_DIR" "$MUSIC_AGENT_DOWNLOAD_DIR/.tmp"
if [[ ! -e "$MUSIC_AGENT_MUSIC_DIR" ]]; then
    install -d -m 0755 -o root -g root "$MUSIC_AGENT_MUSIC_DIR"
fi
[[ -d "$MUSIC_AGENT_MUSIC_DIR" ]] || music_agent_die "$MUSIC_AGENT_MUSIC_DIR is not a directory"
[[ ! -L "$MUSIC_AGENT_MUSIC_DIR" ]] || music_agent_die "$MUSIC_AGENT_MUSIC_DIR must not be a symlink"

if [[ ! -f "$MUSIC_AGENT_ENV_FILE" ]]; then
    install -m 0640 -o root -g "$MUSIC_AGENT_SERVICE_GROUP" "$SCRIPT_DIR/../.env.example" "$MUSIC_AGENT_ENV_FILE"
    music_agent_log "created $MUSIC_AGENT_ENV_FILE"
else
    chown root:"$MUSIC_AGENT_SERVICE_GROUP" "$MUSIC_AGENT_ENV_FILE"
    chmod 0640 "$MUSIC_AGENT_ENV_FILE"
    music_agent_log "preserved existing $MUSIC_AGENT_ENV_FILE"
fi

music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
music_agent_assert_managed_production_config
if ! env -i "PATH=/usr/bin:/bin" "${MUSIC_AGENT_CONFIG_ENV[@]}" \
        "$SCRIPT_DIR/validate-runtime-environment.sh"; then
    music_agent_die "set an operator-controlled MusicBrainz contact in $MUSIC_AGENT_ENV_FILE, then rerun install"
fi

if [[ ! -s "$MUSIC_AGENT_CREDENTIAL_DIR/auth_hmac_key" ]]; then
    "$SCRIPT_DIR/set-secret.sh" auth_hmac_key --generate
fi
for optional_credential in openai_api_key listenbrainz_token; do
    if [[ ! -e "$MUSIC_AGENT_CREDENTIAL_DIR/$optional_credential" ]]; then
        install -m 0600 -o root -g root /dev/null "$MUSIC_AGENT_CREDENTIAL_DIR/$optional_credential"
    fi
done

music_agent_install_pinned_tools "$SCRIPT_DIR/../requirements/tool-pins.env"
"$SCRIPT_DIR/configure-library-acl.sh" "${navidrome_args[@]}"

"$SCRIPT_DIR/deploy.sh" "${deploy_args[@]}"

if [[ "$start_services" -eq 1 ]]; then
    music_agent_systemctl enable music-agent-web.service music-agent-worker.service music-agent-backup.timer
    music_agent_systemctl start music-agent-backup.timer
fi
music_agent_log "installation complete; configured port: 8787 (LAN default: http://music-server:8787)"
music_agent_log "set the OpenAI key with: sudo $MUSIC_AGENT_CURRENT_LINK/scripts/set-secret.sh openai_api_key"
