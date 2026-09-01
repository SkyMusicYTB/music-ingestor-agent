#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage:
  sudo scripts/set-secret.sh auth_hmac_key --generate [--restart]
  sudo scripts/set-secret.sh NAME [--stdin] [--restart]

NAME is auth_hmac_key, openai_api_key, or listenbrainz_token. Secret values are
never accepted as command-line arguments. Interactive input is hidden.
EOF
}

[[ $# -ge 1 ]] || { usage >&2; exit 64; }
secret_name="$1"; shift
case "$secret_name" in
    auth_hmac_key|openai_api_key|listenbrainz_token) ;;
    *) music_agent_die "unsupported secret name" ;;
esac
generate=0 use_stdin=0 restart=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --generate) generate=1; shift ;;
        --stdin) use_stdin=1; shift ;;
        --restart) restart=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ "$generate" -eq 0 || "$secret_name" == "auth_hmac_key" ]] || music_agent_die "only auth_hmac_key can be generated"
[[ "$generate" -eq 0 || "$use_stdin" -eq 0 ]] || music_agent_die "--generate and --stdin are mutually exclusive"

music_agent_require_root
music_agent_acquire_lock operations
install -d -m 0700 -o root -g root "$MUSIC_AGENT_CREDENTIAL_DIR"
temporary="$(mktemp "$MUSIC_AGENT_CREDENTIAL_DIR/.${secret_name}.XXXXXX")"
target="$MUSIC_AGENT_CREDENTIAL_DIR/$secret_name"
previous=""
if [[ -f "$target" ]]; then
    previous="$(mktemp "$MUSIC_AGENT_CREDENTIAL_DIR/.${secret_name}.previous.XXXXXX")"
    install -m 0600 -o root -g root "$target" "$previous"
fi
cleanup() {
    [[ ! -e "$temporary" ]] || find "$temporary" -maxdepth 0 -type f -delete
    [[ -z "$previous" || ! -e "$previous" ]] || find "$previous" -maxdepth 0 -type f -delete
}
trap cleanup EXIT

if [[ "$generate" -eq 1 ]]; then
    openssl rand -hex 32 > "$temporary"
elif [[ "$use_stdin" -eq 1 || ! -t 0 ]]; then
    IFS= read -r secret_value || true
    printf '%s\n' "$secret_value" > "$temporary"
    unset secret_value
else
    IFS= read -r -s -p "New $secret_name: " secret_value
    printf '\n' >&2
    printf '%s\n' "$secret_value" > "$temporary"
    unset secret_value
fi
[[ -s "$temporary" ]] || music_agent_die "secret must not be empty"
if [[ "$secret_name" == "auth_hmac_key" && "$(wc -c < "$temporary")" -lt 33 ]]; then
    music_agent_die "auth_hmac_key must contain at least 32 bytes"
fi
chown root:root "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$target"

if [[ "$restart" -eq 1 ]] && music_agent_unit_exists music-agent-web.service; then
    if ! music_agent_systemctl restart music-agent-web.service; then
        if [[ -n "$previous" ]]; then
            music_agent_warn "web restart failed; restoring the previous credential"
            mv -f "$previous" "$target"
            music_agent_systemctl restart music-agent-web.service || true
        fi
        music_agent_die "credential activation failed"
    fi
fi
trap - EXIT
cleanup
music_agent_log "$secret_name stored as a root-only systemd credential"
