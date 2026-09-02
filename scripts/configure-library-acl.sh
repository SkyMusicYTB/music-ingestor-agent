#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
readonly ACL_ACCESS_PROBE="$SCRIPT_DIR/lib/library_access_probe.py"
readonly ACL_SNAPSHOT_GUARD="$SCRIPT_DIR/lib/acl_snapshot_guard.py"

usage() {
    cat <<'EOF'
Usage: sudo scripts/configure-library-acl.sh [--navidrome-unit UNIT | --navidrome-user USER]

Adds named POSIX ACLs without changing ownership or restarting Navidrome. The
pre-change recursive ACL is saved under /var/lib/music-agent-safety-backups/acl.
EOF
}

navidrome_unit="" navidrome_user=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --navidrome-unit) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; navidrome_unit="$2"; shift 2 ;;
        --navidrome-user) [[ $# -ge 2 ]] || { usage >&2; exit 64; }; navidrome_user="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 64 ;;
    esac
done
[[ -z "$navidrome_unit" || -z "$navidrome_user" ]] || music_agent_die "choose either a unit or a user override"
[[ -z "$navidrome_unit" || "$navidrome_unit" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || music_agent_die "invalid unit name"
[[ -z "$navidrome_user" || "$navidrome_user" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || music_agent_die "invalid user name"

music_agent_require_root
music_agent_assert_supported_host
music_agent_acquire_lock operations
music_agent_require_command getfacl
music_agent_require_command setfacl
music_agent_require_command runuser
[[ -r "$ACL_ACCESS_PROBE" ]] || music_agent_die "ACL access probe is missing: $ACL_ACCESS_PROBE"
[[ -r "$ACL_SNAPSHOT_GUARD" ]] || music_agent_die "ACL snapshot guard is missing: $ACL_SNAPSHOT_GUARD"
music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
music_agent_assert_managed_production_config
[[ -d "$MUSIC_AGENT_MUSIC_DIR" ]] || music_agent_die "music directory does not exist: $MUSIC_AGENT_MUSIC_DIR"
id "$MUSIC_AGENT_SERVICE_USER" >/dev/null 2>&1 || music_agent_die "service account does not exist"

if [[ -z "$navidrome_user" && -z "$navidrome_unit" && "${MUSIC_AGENT_TEST_MODE:-0}" != "1" ]]; then
    mapfile -t navidrome_units < <(
        systemctl list-unit-files --type=service --no-legend 'navidrome*.service' 2>/dev/null |
            awk 'NF {print $1}' | sort -u
    )
    if [[ "${#navidrome_units[@]}" -gt 1 ]]; then
        music_agent_die "multiple Navidrome units detected; pass --navidrome-unit explicitly"
    fi
    if [[ "${#navidrome_units[@]}" -eq 1 ]]; then
        navidrome_unit="${navidrome_units[0]}"
    fi
fi
if [[ -z "$navidrome_user" && -z "$navidrome_unit" && "${MUSIC_AGENT_TEST_MODE:-0}" != "1" ]]; then
    music_agent_die "could not detect Navidrome; pass --navidrome-unit or --navidrome-user"
fi
if [[ -n "$navidrome_unit" && -z "$navidrome_user" ]]; then
    music_agent_unit_exists "$navidrome_unit" || music_agent_die "Navidrome unit not found: $navidrome_unit"
    navidrome_user="$(systemctl show --property=User --value "$navidrome_unit")"
    [[ -n "$navidrome_user" ]] || navidrome_user="root"
fi
[[ -z "$navidrome_user" || "$navidrome_user" == "root" || "$navidrome_user" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] ||
    music_agent_die "Navidrome unit reported an unsafe user name"
if [[ -n "$navidrome_user" && "$navidrome_user" != "root" ]]; then
    id "$navidrome_user" >/dev/null 2>&1 || music_agent_die "Navidrome service user does not exist: $navidrome_user"
    [[ "$navidrome_user" != "$MUSIC_AGENT_SERVICE_USER" ]] ||
        music_agent_die "Navidrome and Music Agent must use distinct service accounts"
fi

original_owner="$(stat -c '%u:%g' "$MUSIC_AGENT_MUSIC_DIR")"
music_agent_prepare_transaction_backup_dir
acl_rollback_dir="$MUSIC_AGENT_TRANSACTION_BACKUP_DIR/acl"
[[ ! -L "$acl_rollback_dir" ]] || music_agent_die "ACL rollback directory must not be a symlink"
install -d -m 0700 -o root -g root "$acl_rollback_dir"
[[ "$(stat -c '%U:%G:%a' "$acl_rollback_dir")" == "root:root:700" ]] ||
    music_agent_die "ACL rollback directory must be root:root mode 0700"

acl_snapshot_name="music-$(date -u +%Y%m%dT%H%M%SZ).$("$MUSIC_AGENT_PYTHON" -c 'import secrets; print(secrets.token_hex(16))').acl"
acl_backup="$acl_rollback_dir/$acl_snapshot_name"
install -m 0600 -o root -g root /dev/null "$acl_backup"
if ! find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
        getfacl --absolute-names --physical --all-effective {} + > "$acl_backup"; then
    unlink "$acl_backup" || true
    music_agent_die "could not capture the pre-change ACL snapshot"
fi

acl_rollback_required=1
rollback_acl_on_exit() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ "$acl_rollback_required" -eq 1 ]]; then
        [[ "$status" -ne 0 ]] || status=1
        if setfacl --restore="$acl_backup"; then
            music_agent_warn "ACL configuration failed; restored snapshot: $acl_backup"
        else
            music_agent_warn "ACL configuration failed and automatic ACL restoration failed; protected snapshot retained: $acl_backup"
            status=1
        fi
    fi
    exit "$status"
}
trap rollback_acl_on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

guard_args=("$acl_backup" --mutable-user "$MUSIC_AGENT_SERVICE_USER")
if [[ -n "$navidrome_user" && "$navidrome_user" != "root" ]]; then
    guard_args+=(--mutable-user "$navidrome_user")
fi
"$MUSIC_AGENT_PYTHON" "$ACL_SNAPSHOT_GUARD" "${guard_args[@]}" ||
    music_agent_die "existing ACL masks contain dormant permissions; no ACL changes were retained"

find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
    setfacl --modify "u:$MUSIC_AGENT_SERVICE_USER:rwX" {} +
find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev -type d -exec \
    setfacl --modify "d:u:$MUSIC_AGENT_SERVICE_USER:rwx" {} +
if [[ -n "$navidrome_user" && "$navidrome_user" != "root" ]]; then
    find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
        setfacl --modify "u:$navidrome_user:r-X" {} +
    find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev -type d -exec \
        setfacl --modify "d:u:$navidrome_user:r-x" {} +
fi

[[ "$(stat -c '%u:%g' "$MUSIC_AGENT_MUSIC_DIR")" == "$original_owner" ]] ||
    music_agent_die "library ownership changed unexpectedly"

verify_account_access() {
    local account="${1:?account required}" operation="${2:?operation required}"
    # The root shell opens the verifier before runuser. This keeps verification
    # working when the trusted deployment checkout is inside a non-traversable home.
    if ! runuser -u "$account" -- "$MUSIC_AGENT_PYTHON" - \
            "$operation" "$MUSIC_AGENT_MUSIC_DIR" < "$ACL_ACCESS_PROBE"; then
        music_agent_die "$operation access verification failed for account: $account"
    fi
}

verify_account_access "$MUSIC_AGENT_SERVICE_USER" write
if [[ -n "$navidrome_user" ]]; then
    verify_account_access "$navidrome_user" read
fi
music_agent_log "library ACL configured without chown or service restart; backup: $acl_backup"
acl_rollback_required=0
