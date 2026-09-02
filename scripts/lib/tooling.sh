# Versioned third-party executable installation helpers.
# shellcheck shell=bash

music_agent_normalize_tool_version() {
    local provider="${1:?provider required}" version="${2:?version required}"
    local provider_dir="$MUSIC_AGENT_TOOLS_DIR/$provider"
    local version_dir="$provider_dir/$version"
    local bin_dir="$version_dir/bin"
    local executable="$bin_dir/$provider" file relative
    case "$provider" in
        yt-dlp)
            [[ "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]] ||
                music_agent_die "invalid managed yt-dlp directory: $version"
            ;;
        deno)
            [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
                music_agent_die "invalid managed Deno directory: $version"
            ;;
        *) music_agent_die "unknown managed tool provider: $provider" ;;
    esac
    for file in "$provider_dir" "$version_dir" "$bin_dir"; do
        [[ ! -e "$file" || ( -d "$file" && ! -L "$file" ) ]] ||
            music_agent_die "managed tool directory is not a physical directory: $file"
        install -d -m 0755 -o root -g root "$file"
        chown root:root "$file"
        chmod 0755 "$file"
    done
    if [[ -e "$executable" ]]; then
        [[ -f "$executable" && ! -L "$executable" ]] ||
            music_agent_die "managed tool executable is not a regular file: $executable"
        chown root:root "$executable"
        chmod 0755 "$executable"
    fi
    for file in "$version_dir/artifact.sha256" "$version_dir/binary.sha256"; do
        if [[ -e "$file" ]]; then
            [[ -f "$file" && ! -L "$file" ]] ||
                music_agent_die "managed tool manifest is not a regular file: $file"
            chown root:root "$file"
            chmod 0644 "$file"
        fi
    done
    while IFS= read -r -d '' file; do
        relative="${file#"$version_dir"/}"
        case "$relative" in
            bin|"bin/$provider"|artifact.sha256|binary.sha256) ;;
            *) music_agent_die "unexpected content in managed tool version: $file" ;;
        esac
    done < <(find "$version_dir" -mindepth 1 -maxdepth 2 -print0)
}

music_agent_normalize_tool_tree() {
    local directory provider version_dir version link target entry
    for directory in "$MUSIC_AGENT_OPT_DIR" "$MUSIC_AGENT_TOOLS_DIR"; do
        [[ ! -e "$directory" || ( -d "$directory" && ! -L "$directory" ) ]] ||
            music_agent_die "managed tool parent is not a physical directory: $directory"
        install -d -m 0755 -o root -g root "$directory"
        chown root:root "$directory"
        chmod 0755 "$directory"
    done
    for directory in "$MUSIC_AGENT_TOOLS_DIR/current" "$MUSIC_AGENT_TOOL_BIN"; do
        [[ ! -e "$directory" || ( -d "$directory" && ! -L "$directory" ) ]] ||
            music_agent_die "managed tool link parent is not a physical directory: $directory"
        install -d -m 0755 -o root -g root "$directory"
        chown root:root "$directory"
        chmod 0755 "$directory"
    done
    while IFS= read -r -d '' entry; do
        [[ "$entry" == "$MUSIC_AGENT_TOOL_BIN" ]] ||
            music_agent_die "managed tool current directory contains an unexpected entry: $entry"
    done < <(find "$MUSIC_AGENT_TOOLS_DIR/current" -mindepth 1 -maxdepth 1 -print0)
    while IFS= read -r -d '' entry; do
        case "${entry##*/}" in
            yt-dlp|deno) ;;
            *) music_agent_die "managed tool bin contains an unexpected entry: $entry" ;;
        esac
    done < <(find "$MUSIC_AGENT_TOOL_BIN" -mindepth 1 -maxdepth 1 -print0)
    for provider in yt-dlp deno; do
        directory="$MUSIC_AGENT_TOOLS_DIR/$provider"
        if [[ ! -e "$directory" ]]; then
            continue
        fi
        [[ -d "$directory" && ! -L "$directory" ]] ||
            music_agent_die "managed provider path is not a physical directory: $directory"
        chown root:root "$directory"
        chmod 0755 "$directory"
        while IFS= read -r -d '' version_dir; do
            version="${version_dir##*/}"
            music_agent_normalize_tool_version "$provider" "$version"
        done < <(find "$directory" -mindepth 1 -maxdepth 1 -type d -print0)
        if [[ -n "$(find "$directory" -mindepth 1 -maxdepth 1 ! -type d -print -quit)" ]]; then
            music_agent_die "managed provider directory contains an unexpected entry: $directory"
        fi
    done
    for link in "$MUSIC_AGENT_TOOL_BIN/yt-dlp" "$MUSIC_AGENT_TOOL_BIN/deno"; do
        if [[ ! -e "$link" && ! -L "$link" ]]; then
            continue
        fi
        [[ -L "$link" ]] || music_agent_die "managed tool link is not a symlink: $link"
        target="$(readlink -f "$link")"
        case "${link##*/}" in
            yt-dlp) music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/yt-dlp" ;;
            deno) music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/deno" ;;
        esac
        [[ -f "$target" && -x "$target" ]] ||
            music_agent_die "managed tool link target is not executable: $link"
        chown -h root:root "$link"
    done
}

music_agent_probe_tools_as_service() {
    music_agent_require_command runuser
    runuser -u "$MUSIC_AGENT_SERVICE_USER" -- env -i \
        "PATH=$MUSIC_AGENT_PATH" \
        "HOME=$MUSIC_AGENT_STATE_DIR" \
        "PYTHONDONTWRITEBYTECODE=1" \
        "$MUSIC_AGENT_PYTHON" - <<'PY'
import os
import shutil
import subprocess

for name in ("yt-dlp", "deno"):
    executable = shutil.which(name)
    if executable is None:
        raise SystemExit(f"{name} is not available on the service PATH")
    completed = subprocess.run(
        [executable, "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ["HOME"],
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SystemExit(f"{name} could not execute as the service account")
    print(f"{name}={completed.stdout.splitlines()[0][:100]}")
PY
}

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
    trap 'if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then find "$temp_dir" -depth -delete; fi' EXIT
    music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/yt-dlp"
    music_agent_normalize_tool_version yt-dlp "$version"
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
        install -d -m 0755 -o root -g root "$target_dir"
        install -m 0755 -o root -g root "$downloaded" "$target"
        printf '%s\n' "$digest" > "$digest_file"
        printf '%s\n' "$digest" > "$binary_digest_file"
        chown root:root "$digest_file" "$binary_digest_file"
        chmod 0644 "$digest_file" "$binary_digest_file"
    fi
    music_agent_normalize_tool_version yt-dlp "$version"
    install -d -m 0755 -o root -g root "$MUSIC_AGENT_TOOL_BIN"
    music_agent_atomic_symlink "$target" "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
    chown -h root:root "$MUSIC_AGENT_TOOL_BIN/yt-dlp"
)

music_agent_install_deno() (
    local version="${1:?version required}" url="${2:?URL required}" digest="${3:?digest required}"
    local version_dir="$MUSIC_AGENT_TOOLS_DIR/deno/$version"
    local target="$version_dir/bin/deno" digest_file="$version_dir/artifact.sha256"
    local binary_digest_file="$version_dir/binary.sha256"
    local temp_dir="" archive actual_version
    trap 'if [[ -n "$temp_dir" && -d "$temp_dir" ]]; then find "$temp_dir" -depth -delete; fi' EXIT
    music_agent_assert_within "$target" "$MUSIC_AGENT_TOOLS_DIR/deno"
    if [[ -e "$version_dir" ]]; then
        music_agent_normalize_tool_version deno "$version"
    fi
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
        install -d -m 0755 -o root -g root "$version_dir/bin"
        install -m 0755 -o root -g root "$temp_dir/unpacked/deno" "$target"
        printf '%s\n' "$digest" > "$digest_file"
        music_agent_sha256 "$target" > "$binary_digest_file"
        chown root:root "$digest_file" "$binary_digest_file"
        chmod 0644 "$digest_file" "$binary_digest_file"
    fi
    music_agent_normalize_tool_version deno "$version"
    install -d -m 0755 -o root -g root "$MUSIC_AGENT_TOOL_BIN"
    music_agent_atomic_symlink "$target" "$MUSIC_AGENT_TOOL_BIN/deno"
    chown -h root:root "$MUSIC_AGENT_TOOL_BIN/deno"
)

music_agent_install_pinned_tools() {
    local pin_file="${1:?pin file required}"
    music_agent_require_command curl
    music_agent_require_command unzip
    music_agent_normalize_tool_tree
    music_agent_read_tool_pins "$pin_file"
    music_agent_install_deno "$DENO_VERSION" "$DENO_URL" "$DENO_SHA256"
    music_agent_install_yt_dlp "$YT_DLP_VERSION" "$YT_DLP_URL" "$YT_DLP_SHA256"
    music_agent_normalize_tool_tree
    music_agent_probe_tools_as_service >/dev/null
}
