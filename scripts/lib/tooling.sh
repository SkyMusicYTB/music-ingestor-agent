# Versioned third-party executable installation helpers.
# shellcheck shell=bash

music_agent_read_tool_pins() {
    local pin_file="${1:?pin file required}" raw key value
    [[ -r "$pin_file" ]] || music_agent_die "tool pin file is missing: $pin_file"
    YT_DLP_VERSION="" YT_DLP_URL="" YT_DLP_SHA256=""
    DENO_VERSION="" DENO_URL="" DENO_SHA256=""
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        [[ -z "$raw" || "${raw:0:1}" == "#" ]] && continue
        [[ "$raw" == *"="* ]] || music_agent_die "invalid tool pin line"
        key="${raw%%=*}"
        value="${raw#*=}"
        case "$key" in
            YT_DLP_VERSION) YT_DLP_VERSION="$value" ;;
            YT_DLP_URL) YT_DLP_URL="$value" ;;
            YT_DLP_SHA256) YT_DLP_SHA256="$value" ;;
            DENO_VERSION) DENO_VERSION="$value" ;;
            DENO_URL) DENO_URL="$value" ;;
            DENO_SHA256) DENO_SHA256="$value" ;;
            *) music_agent_die "unknown key in tool pin file: $key" ;;
        esac
    done < "$pin_file"
    [[ "$YT_DLP_VERSION" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]] || music_agent_die "invalid yt-dlp version pin"
    [[ "$DENO_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || music_agent_die "invalid Deno version pin"
    [[ "$YT_DLP_URL" == "https://github.com/yt-dlp/yt-dlp/releases/download/$YT_DLP_VERSION/yt-dlp" ]] ||
        music_agent_die "yt-dlp URL does not match the pinned official release"
    [[ "$DENO_URL" == "https://github.com/denoland/deno/releases/download/v$DENO_VERSION/deno-x86_64-unknown-linux-gnu.zip" ]] ||
        music_agent_die "Deno URL does not match the pinned official release"
    [[ "$YT_DLP_SHA256" =~ ^[0-9a-f]{64}$ && "$DENO_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
        music_agent_die "invalid tool SHA-256 pin"
}

music_agent_download() {
    local url="${1:?URL required}" output="${2:?output required}"
    curl --proto '=https' --tlsv1.2 --location --fail --silent --show-error \
        --retry 3 --retry-all-errors --connect-timeout 20 --max-time 600 \
        --output "$output" "$url"
}

music_agent_install_yt_dlp() (
    local version="${1:?version required}" url="${2:?URL required}" digest="${3:?digest required}"
    local target_dir="$MUSIC_AGENT_TOOLS_DIR/yt-dlp/$version/bin"
    local version_dir="$MUSIC_AGENT_TOOLS_DIR/yt-dlp/$version"
    local target="$target_dir/yt-dlp" digest_file="$version_dir/artifact.sha256"
    local binary_digest_file="$version_dir/binary.sha256"
    local temp_dir="" downloaded actual_version
    # shellcheck disable=SC2329 # invoked indirectly by the EXIT trap
    cleanup_yt_dlp_download() {
        if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then
            find "$temp_dir" -depth -delete
        fi
    }
    trap cleanup_yt_dlp_download EXIT
    music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/yt-dlp"
    if [[ -f "$target" ]]; then
        music_agent_verify_sha256 "$target" "$digest"
        [[ -r "$digest_file" && "$(<"$digest_file")" == "$digest" ]] ||
            music_agent_die "installed yt-dlp provenance is inconsistent"
        [[ -r "$binary_digest_file" && "$(<"$binary_digest_file")" == "$digest" ]] ||
            music_agent_die "installed yt-dlp binary digest is inconsistent"
    else
        temp_dir="$(mktemp -d)"
        downloaded="$temp_dir/yt-dlp"
        music_agent_download "$url" "$downloaded"
        music_agent_verify_sha256 "$downloaded" "$digest"
        chmod 0755 "$downloaded"
        actual_version="$(PATH=/usr/bin:/bin "$downloaded" --version)"
        [[ "$actual_version" == "$version" ]] || music_agent_die "yt-dlp reported unexpected version: $actual_version"
        install -d -m 0755 "$target_dir"
        install -m 0755 -o root -g root "$downloaded" "$target"
        printf '%s\n' "$digest" > "$digest_file"
        printf '%s\n' "$digest" > "$binary_digest_file"
        chown root:root "$digest_file" "$binary_digest_file"
        chmod 0644 "$digest_file" "$binary_digest_file"
    fi
    install -d -m 0755 "$MUSIC_AGENT_TOOL_BIN"
    music_agent_atomic_symlink "$target" "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
)

music_agent_install_deno() (
    local version="${1:?version required}" url="${2:?URL required}" digest="${3:?digest required}"
    local version_dir="$MUSIC_AGENT_TOOLS_DIR/deno/$version"
    local target="$version_dir/bin/deno" digest_file="$version_dir/artifact.sha256"
    local binary_digest_file="$version_dir/binary.sha256"
    local temp_dir="" archive actual_version
    # shellcheck disable=SC2329 # invoked indirectly by the EXIT trap
    cleanup_deno_download() {
        if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then
            find "$temp_dir" -depth -delete
        fi
    }
    trap cleanup_deno_download EXIT
    music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/deno"
    if [[ -x "$target" && -r "$digest_file" && -r "$binary_digest_file" ]] &&
            [[ "$(<"$digest_file")" == "$digest" ]]; then
        music_agent_verify_sha256 "$target" "$(<"$binary_digest_file")"
        actual_version="$("$target" --version | awk 'NR == 1 {print $2}')"
        [[ "$actual_version" == "$version" ]] || music_agent_die "installed Deno version is inconsistent"
    else
        [[ ! -e "$version_dir" ]] || music_agent_die "refusing to replace inconsistent Deno directory: $version_dir"
        temp_dir="$(mktemp -d)"
        archive="$temp_dir/deno.zip"
        music_agent_download "$url" "$archive"
        music_agent_verify_sha256 "$archive" "$digest"
        unzip -q "$archive" -d "$temp_dir/unpacked"
        [[ -x "$temp_dir/unpacked/deno" ]] || music_agent_die "Deno archive did not contain the expected executable"
        actual_version="$("$temp_dir/unpacked/deno" --version | awk 'NR == 1 {print $2}')"
        [[ "$actual_version" == "$version" ]] || music_agent_die "Deno reported unexpected version: $actual_version"
        install -d -m 0755 "$version_dir/bin"
        install -m 0755 -o root -g root "$temp_dir/unpacked/deno" "$target"
        printf '%s\n' "$digest" > "$digest_file"
        music_agent_sha256 "$target" > "$binary_digest_file"
        chown root:root "$digest_file" "$binary_digest_file"
        chmod 0644 "$digest_file" "$binary_digest_file"
    fi
    install -d -m 0755 "$MUSIC_AGENT_TOOL_BIN"
    music_agent_atomic_symlink "$target" "$MUSIC_AGENT_TOOL_BIN/deno"
)

music_agent_install_pinned_tools() {
    local pin_file="${1:?pin file required}"
    music_agent_require_command curl
    music_agent_require_command unzip
    music_agent_read_tool_pins "$pin_file"
    music_agent_install_deno "$DENO_VERSION" "$DENO_URL" "$DENO_SHA256"
    music_agent_install_yt_dlp "$YT_DLP_VERSION" "$YT_DLP_URL" "$YT_DLP_SHA256"
}
