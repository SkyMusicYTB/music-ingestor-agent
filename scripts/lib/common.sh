# Shared safety primitives for root-run Music Agent operations.
# shellcheck shell=bash disable=SC2034

readonly MUSIC_AGENT_SERVICE_USER="music-agent"
readonly MUSIC_AGENT_SERVICE_GROUP="music-agent"
readonly MUSIC_AGENT_SUPPORTED_UBUNTU="26.04"
readonly MUSIC_AGENT_SUPPORTED_ARCH="amd64"
readonly MUSIC_AGENT_PYTHON="/usr/bin/python3"
readonly MUSIC_AGENT_NATIVE_DATABASE_PATH="/var/lib/music-agent/music-agent.db"
readonly MUSIC_AGENT_NATIVE_ARTWORK_PATH="/var/lib/music-agent/artwork"
readonly MUSIC_AGENT_NATIVE_DOWNLOADS_PATH="/srv/music-downloads"
readonly MUSIC_AGENT_NATIVE_MUSIC_PATH="/srv/music"
readonly MUSIC_AGENT_NATIVE_BACKUP_PATH="/var/lib/music-agent/backups"
MUSIC_AGENT_DIRECTORY_WRITE_DENIAL_PROBE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/directory_write_denial_probe.py"
readonly MUSIC_AGENT_DIRECTORY_WRITE_DENIAL_PROBE

if [[ "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]]; then
    : "${MUSIC_AGENT_ROOT_PREFIX:?MUSIC_AGENT_ROOT_PREFIX is required in test mode}"
    if [[ "$MUSIC_AGENT_ROOT_PREFIX" != /* || "$MUSIC_AGENT_ROOT_PREFIX" == "/" ]]; then
        printf 'unsafe MUSIC_AGENT_ROOT_PREFIX: %s\n' "$MUSIC_AGENT_ROOT_PREFIX" >&2
        exit 64
    fi
else
    readonly MUSIC_AGENT_ROOT_PREFIX=""
fi

music_agent_path() {
    local path="${1:?path required}"
    [[ "$path" == /* ]] || {
        printf 'internal error: expected absolute path, got %s\n' "$path" >&2
        return 64
    }
    printf '%s%s\n' "$MUSIC_AGENT_ROOT_PREFIX" "$path"
}

MUSIC_AGENT_OPT_DIR="$(music_agent_path /opt/music-agent)"
readonly MUSIC_AGENT_OPT_DIR
readonly MUSIC_AGENT_RELEASES_DIR="$MUSIC_AGENT_OPT_DIR/releases"
readonly MUSIC_AGENT_CURRENT_LINK="$MUSIC_AGENT_OPT_DIR/current"
readonly MUSIC_AGENT_TOOLS_DIR="$MUSIC_AGENT_OPT_DIR/tools"
readonly MUSIC_AGENT_TOOL_BIN="$MUSIC_AGENT_TOOLS_DIR/current/bin"
MUSIC_AGENT_ETC_DIR="$(music_agent_path /etc/music-agent)"
readonly MUSIC_AGENT_ETC_DIR
readonly MUSIC_AGENT_ENV_FILE="$MUSIC_AGENT_ETC_DIR/music-agent.env"
readonly MUSIC_AGENT_CREDENTIAL_DIR="$MUSIC_AGENT_ETC_DIR/credentials"
MUSIC_AGENT_STATE_DIR="$(music_agent_path /var/lib/music-agent)"
readonly MUSIC_AGENT_STATE_DIR
readonly MUSIC_AGENT_DB="$MUSIC_AGENT_STATE_DIR/music-agent.db"
readonly MUSIC_AGENT_BACKUP_DIR="$MUSIC_AGENT_STATE_DIR/backups"
readonly MUSIC_AGENT_DEPLOYMENT_DIR="$MUSIC_AGENT_STATE_DIR/deployments"
MUSIC_AGENT_TRANSACTION_BACKUP_DIR="$(music_agent_path /var/lib/music-agent-safety-backups)"
readonly MUSIC_AGENT_TRANSACTION_BACKUP_DIR
MUSIC_AGENT_DOWNLOAD_DIR="$(music_agent_path /srv/music-downloads)"
readonly MUSIC_AGENT_DOWNLOAD_DIR
MUSIC_AGENT_MUSIC_DIR="$(music_agent_path /srv/music)"
readonly MUSIC_AGENT_MUSIC_DIR
MUSIC_AGENT_UNIT_DIR="$(music_agent_path /etc/systemd/system)"
readonly MUSIC_AGENT_UNIT_DIR
MUSIC_AGENT_LOCK_DIR="$(music_agent_path /run/lock)"
readonly MUSIC_AGENT_LOCK_DIR
readonly MUSIC_AGENT_PATH="$MUSIC_AGENT_TOOL_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

music_agent_log() {
    printf '[music-agent] %s\n' "$*" >&2
}

music_agent_warn() {
    printf '[music-agent] WARNING: %s\n' "$*" >&2
}

music_agent_die() {
    printf '[music-agent] ERROR: %s\n' "$*" >&2
    exit 1
}

music_agent_require_root() {
    if [[ "${MUSIC_AGENT_TEST_MODE:-0}" != "1" && "$EUID" -ne 0 ]]; then
        music_agent_die "run this command as root (for example, with sudo)"
    fi
}

music_agent_require_command() {
    command -v "${1:?command required}" >/dev/null 2>&1 || music_agent_die "required command not found: $1"
}

music_agent_assert_supported_host() {
    if [[ "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]]; then
        return 0
    fi
    [[ "$(uname -s)" == "Linux" ]] || music_agent_die "production deployment requires Linux"

    local os_release=/etc/os-release
    [[ -r "$os_release" ]] || music_agent_die "cannot read $os_release"
    local distro_id="" version_id=""
    while IFS='=' read -r key value; do
        value="${value%\"}"
        value="${value#\"}"
        case "$key" in
            ID) distro_id="$value" ;;
            VERSION_ID) version_id="$value" ;;
        esac
    done < "$os_release"
    [[ "$distro_id" == "ubuntu" && "$version_id" == "$MUSIC_AGENT_SUPPORTED_UBUNTU" ]] ||
        music_agent_die "supported production host is Ubuntu $MUSIC_AGENT_SUPPORTED_UBUNTU; found ${distro_id:-unknown} ${version_id:-unknown}"

    music_agent_require_command dpkg
    local arch
    arch="$(dpkg --print-architecture)"
    [[ "$arch" == "$MUSIC_AGENT_SUPPORTED_ARCH" ]] ||
        music_agent_die "supported architecture is $MUSIC_AGENT_SUPPORTED_ARCH; found $arch"

    [[ -x "$MUSIC_AGENT_PYTHON" ]] || music_agent_die "$MUSIC_AGENT_PYTHON is missing"
    "$MUSIC_AGENT_PYTHON" - <<'PY' || music_agent_die "Python 3.14 is required"
import sys
raise SystemExit(0 if sys.version_info[:2] == (3, 14) else 1)
PY
}

music_agent_acquire_lock() {
    local name="${1:?lock name required}"
    [[ "$name" =~ ^[a-z0-9-]+$ ]] || music_agent_die "unsafe lock name: $name"
    local lock_file="$MUSIC_AGENT_LOCK_DIR/music-agent-$name.lock"
    if [[ "${MUSIC_AGENT_OPERATION_LOCK_HELD:-0}" == "1" ]]; then
        if [[ "${MUSIC_AGENT_TEST_MODE:-0}" != "1" ]]; then
            [[ "$(readlink /proc/$$/fd/9 2>/dev/null)" == "$lock_file" ]] ||
                music_agent_die "inherited operation lock is invalid"
        fi
        flock -n 9 || music_agent_die "inherited operation lock is not held"
        return 0
    fi
    [[ -d "$MUSIC_AGENT_LOCK_DIR" ]] || install -d -m 0755 "$MUSIC_AGENT_LOCK_DIR"
    exec 9>"$lock_file"
    flock -n 9 || music_agent_die "another Music Agent $name operation is running"
    export MUSIC_AGENT_OPERATION_LOCK_HELD=1
}

music_agent_acquire_directory_lock() {
    local directory="${1:?lock directory required}"
    local description="${2:-operation}"
    [[ -d "$directory" && ! -L "$directory" ]] ||
        music_agent_die "$description lock target must be a physical directory: $directory"

    # Lock the already-open directory inode.  Never create a lock pathname in a
    # service-writable directory: a privileged invocation would otherwise follow
    # an attacker-planted symlink while opening it for output.
    exec 8<"$directory"
    flock -n 8 || music_agent_die "another $description is running"
}

music_agent_assert_within() {
    local candidate="${1:?candidate required}" parent="${2:?parent required}"
    case "$candidate" in
        "$parent"/*) ;;
        *) music_agent_die "unsafe path outside $parent: $candidate" ;;
    esac
}

music_agent_assert_managed_backup_path() {
    local candidate="${1:?backup path required}"
    case "$candidate" in
        "$MUSIC_AGENT_BACKUP_DIR"/*|"$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"/*) ;;
        *) music_agent_die "unsafe path outside the managed backup directories: $candidate" ;;
    esac
}

music_agent_prepare_transaction_backup_dir() {
    local parent
    parent="$(dirname "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR")"
    [[ ! -L "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR" ]] ||
        music_agent_die "transaction backup directory must not be a symlink"
    if [[ "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]]; then
        install -d -m 0700 "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"
        return 0
    fi
    [[ -d "$parent" && ! -L "$parent" ]] ||
        music_agent_die "transaction backup parent must be a physical directory: $parent"
    [[ "$(stat -c '%U' "$parent")" == "root" ]] ||
        music_agent_die "transaction backup parent must be root-owned: $parent"
    [[ -z "$(find "$parent" -maxdepth 0 -perm /022 -print -quit)" ]] ||
        music_agent_die "transaction backup parent must not be group/world-writable: $parent"
    music_agent_require_command runuser
    [[ -r "$MUSIC_AGENT_DIRECTORY_WRITE_DENIAL_PROBE" ]] ||
        music_agent_die "directory write-denial probe is missing"
    id "$MUSIC_AGENT_SERVICE_USER" >/dev/null 2>&1 ||
        music_agent_die "service account does not exist"
    music_agent_assert_service_cannot_write_directory "$parent"
    install -d -m 0700 -o root -g root "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"
    [[ "$(stat -c '%U:%G:%a' "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR")" == "root:root:700" ]] ||
        music_agent_die "transaction backup directory must be root:root mode 0700"
    music_agent_assert_service_cannot_write_directory "$MUSIC_AGENT_TRANSACTION_BACKUP_DIR"
}

music_agent_assert_service_cannot_write_directory() {
    local directory="${1:?directory required}" nonce probe_name probe_path status
    nonce="$("$MUSIC_AGENT_PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
    [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || music_agent_die "could not create directory probe nonce"
    probe_name=".music-agent-deny-write-$nonce"
    probe_path="$directory/$probe_name"
    set +e
    runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i \
        "PATH=/usr/bin:/bin" "$MUSIC_AGENT_PYTHON" - "$directory" "$probe_name" \
        < "$MUSIC_AGENT_DIRECTORY_WRITE_DENIAL_PROBE"
    status=$?
    set -e
    if [[ -e "$probe_path" || -L "$probe_path" ]]; then
        unlink "$probe_path" || music_agent_die "could not clean directory write-denial probe"
    fi
    [[ "$status" -eq 0 ]] ||
        music_agent_die "service account can mutate protected backup directory entries: $directory"
}

music_agent_atomic_symlink() {
    local target="${1:?target required}" link="${2:?link required}"
    local parent temp
    parent="$(dirname "$link")"
    install -d -m 0755 "$parent"
    temp="$parent/.${link##*/}.new.$$"
    ln -s "$target" "$temp" || return 1
    if ! mv -Tf "$temp" "$link"; then
        unlink "$temp" || true
        return 1
    fi
}

music_agent_current_release() {
    if [[ -L "$MUSIC_AGENT_CURRENT_LINK" ]]; then
        readlink -f "$MUSIC_AGENT_CURRENT_LINK"
    fi
}

music_agent_unit_exists() {
    local unit="${1:?unit required}"
    if [[ "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]]; then
        [[ -f "$MUSIC_AGENT_UNIT_DIR/$unit" ]]
    else
        systemctl cat "$unit" >/dev/null 2>&1
    fi
}

music_agent_systemctl() {
    if [[ "${MUSIC_AGENT_TEST_MODE:-0}" == "1" ]]; then
        music_agent_log "test mode: systemctl $*"
        return 0
    fi
    systemctl "$@"
}

music_agent_stop_services() {
    local unit failed=0
    for unit in music-agent-worker.service music-agent-web.service; do
        if music_agent_unit_exists "$unit"; then
            music_agent_systemctl stop "$unit" || failed=1
        fi
    done
    return "$failed"
}

music_agent_start_services() {
    local failed=0
    music_agent_systemctl start music-agent-web.service || failed=1
    music_agent_systemctl start music-agent-worker.service || failed=1
    return "$failed"
}

music_agent_parse_env_file() {
    local file="${1:?environment file required}" raw key value
    MUSIC_AGENT_CONFIG_ENV=()
    [[ -r "$file" ]] || music_agent_die "configuration file is not readable: $file"
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        raw="${raw#"${raw%%[![:space:]]*}"}"
        [[ -z "$raw" || "${raw:0:1}" == "#" ]] && continue
        [[ "$raw" == *"="* ]] || music_agent_die "invalid line in $file"
        key="${raw%%=*}"
        value="${raw#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        [[ "$key" =~ ^MUSIC_AGENT_[A-Z0-9_]+$ ]] ||
            music_agent_die "only MUSIC_AGENT_* variables are allowed in $file: $key"
        case "$key" in
            MUSIC_AGENT_SERVICE_ROLE|MUSIC_AGENT_CREDENTIAL_DIRECTORY)
                music_agent_die "$key is managed by the systemd units"
                ;;
        esac
        if [[ "$value" == \"*\" && "$value" == *\" ]]; then
            value="${value:1:${#value}-2}"
        elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
            value="${value:1:${#value}-2}"
        fi
        [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || music_agent_die "invalid newline in $file"
        MUSIC_AGENT_CONFIG_ENV+=("$key=$value")
    done < "$file"
}

music_agent_assert_managed_production_config() {
    local required key expected assignment actual matches
    for required in \
            "MUSIC_AGENT_ENVIRONMENT=production" \
            "MUSIC_AGENT_DATABASE_PATH=$MUSIC_AGENT_NATIVE_DATABASE_PATH" \
            "MUSIC_AGENT_ARTWORK_PATH=$MUSIC_AGENT_NATIVE_ARTWORK_PATH" \
            "MUSIC_AGENT_DOWNLOADS_PATH=$MUSIC_AGENT_NATIVE_DOWNLOADS_PATH" \
            "MUSIC_AGENT_MUSIC_PATH=$MUSIC_AGENT_NATIVE_MUSIC_PATH" \
            "MUSIC_AGENT_BACKUP_PATH=$MUSIC_AGENT_NATIVE_BACKUP_PATH"; do
        key="${required%%=*}"
        expected="${required#*=}"
        actual=""
        matches=0
        for assignment in "${MUSIC_AGENT_CONFIG_ENV[@]}"; do
            if [[ "${assignment%%=*}" == "$key" ]]; then
                actual="${assignment#*=}"
                matches=$((matches + 1))
            fi
        done
        [[ "$matches" -eq 1 ]] ||
            music_agent_die "$key must appear exactly once in $MUSIC_AGENT_ENV_FILE"
        [[ "$actual" == "$expected" ]] ||
            music_agent_die "$key must use the managed production value: $expected"
    done
}

music_agent_with_credentials() (
    local command=("$@") credential_tmp credential
    [[ ${#command[@]} -gt 0 ]] || music_agent_die "internal error: command required"
    if music_agent_unit_exists music-agent-worker.service &&
            music_agent_systemctl is-active --quiet music-agent-worker.service; then
        music_agent_die \
            "credential-backed administrative commands require music-agent-worker.service to be stopped"
    fi
    music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
    music_agent_assert_managed_production_config
    [[ -d "$(music_agent_path /run)" ]] || install -d -m 0755 "$(music_agent_path /run)"
    credential_tmp="$(mktemp -d "$(music_agent_path /run)/music-agent-credentials.XXXXXX")"
    trap 'if [[ -n "${credential_tmp:-}" && -d "$credential_tmp" ]]; then find "$credential_tmp" -depth -delete; fi' EXIT
    chown "$MUSIC_AGENT_SERVICE_USER:$MUSIC_AGENT_SERVICE_GROUP" "$credential_tmp"
    chmod 0700 "$credential_tmp"
    for credential in auth_hmac_key openai_api_key listenbrainz_token; do
        if [[ -f "$MUSIC_AGENT_CREDENTIAL_DIR/$credential" ]]; then
            install -m 0400 -o "$MUSIC_AGENT_SERVICE_USER" -g "$MUSIC_AGENT_SERVICE_GROUP" \
                "$MUSIC_AGENT_CREDENTIAL_DIR/$credential" "$credential_tmp/$credential"
        fi
    done
    set +e
    runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i \
        "PATH=$MUSIC_AGENT_PATH" \
        "HOME=$MUSIC_AGENT_STATE_DIR" \
        "CREDENTIALS_DIRECTORY=$credential_tmp" \
        "PYTHONDONTWRITEBYTECODE=1" \
        "${MUSIC_AGENT_CONFIG_ENV[@]}" \
        "MUSIC_AGENT_SERVICE_ROLE=web" \
        "${command[@]}"
    local status=$?
    set -e
    return "$status"
)

music_agent_without_credentials() (
    local command=("$@")
    [[ ${#command[@]} -gt 0 ]] || music_agent_die "internal error: command required"
    music_agent_parse_env_file "$MUSIC_AGENT_ENV_FILE"
    music_agent_assert_managed_production_config
    runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i \
        "PATH=$MUSIC_AGENT_PATH" \
        "HOME=$MUSIC_AGENT_STATE_DIR" \
        "PYTHONDONTWRITEBYTECODE=1" \
        "${MUSIC_AGENT_CONFIG_ENV[@]}" \
        "MUSIC_AGENT_SERVICE_ROLE=worker" \
        "${command[@]}"
)

music_agent_sha256() {
    local file="${1:?file required}"
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$file" | awk '{print $1}'
    else
        shasum -a 256 "$file" | awk '{print $1}'
    fi
}

music_agent_verify_sha256() {
    local file="${1:?file required}" expected="${2:?expected digest required}" actual
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || music_agent_die "invalid SHA-256 value"
    actual="$(music_agent_sha256 "$file")"
    [[ "$actual" == "$expected" ]] || music_agent_die "SHA-256 mismatch for $file"
}
