#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
# shellcheck source=scripts/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
    cat <<'EOF'
Usage: sudo scripts/configure-library-acl.sh [--navidrome-unit UNIT | --navidrome-user USER]

Adds named POSIX ACLs without changing ownership or restarting Navidrome. The
pre-change recursive ACL is saved under /var/lib/music-agent/acl-backups.
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
[[ -d "$MUSIC_AGENT_MUSIC_DIR" ]] || music_agent_die "music directory does not exist: $MUSIC_AGENT_MUSIC_DIR"
id "$MUSIC_AGENT_SERVICE_USER" >/dev/null 2>&1 || music_agent_die "service account does not exist"

if [[ -z "$navidrome_user" && -z "$navidrome_unit" && "${MUSIC_AGENT_TEST_MODE:-0}" != "1" ]]; then
    navidrome_unit="$(systemctl list-unit-files --type=service --no-legend 'navidrome*.service' 2>/dev/null | awk 'NR == 1 {print $1}')"
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
fi

original_owner="$(stat -c '%u:%g' "$MUSIC_AGENT_MUSIC_DIR")"
acl_backup_dir="$MUSIC_AGENT_STATE_DIR/acl-backups"
install -d -m 0700 -o root -g root "$acl_backup_dir"
acl_backup="$(mktemp "$acl_backup_dir/music-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX.acl")"
find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
    getfacl --absolute-names --physical {} + > "$acl_backup"
chmod 0600 "$acl_backup"

find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
    setfacl --modify "u:$MUSIC_AGENT_SERVICE_USER:rwX" {} +
find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev -type d -exec \
    setfacl --modify "d:u:$MUSIC_AGENT_SERVICE_USER:rwx,d:m::rwx" {} +
if [[ -n "$navidrome_user" && "$navidrome_user" != "root" ]]; then
    find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev \( -type f -o -type d \) -exec \
        setfacl --modify "u:$navidrome_user:r-X" {} +
    find -P "$MUSIC_AGENT_MUSIC_DIR" -xdev -type d -exec \
        setfacl --modify "d:u:$navidrome_user:r-x,d:m::rwx" {} +
fi

[[ "$(stat -c '%u:%g' "$MUSIC_AGENT_MUSIC_DIR")" == "$original_owner" ]] ||
    music_agent_die "library ownership changed unexpectedly"
runuser -u "$MUSIC_AGENT_SERVICE_USER" -- test -r "$MUSIC_AGENT_MUSIC_DIR"
runuser -u "$MUSIC_AGENT_SERVICE_USER" -- test -w "$MUSIC_AGENT_MUSIC_DIR"
if [[ -n "$navidrome_user" && "$navidrome_user" != "root" ]]; then
    runuser -u "$navidrome_user" -- test -r "$MUSIC_AGENT_MUSIC_DIR"
fi
music_agent_log "library ACL configured without chown or service restart; backup: $acl_backup"
