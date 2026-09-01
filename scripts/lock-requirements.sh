#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
REPO_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly REPO_DIR
readonly PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    printf 'Python not found: %s\n' "$PYTHON_BIN" >&2
    exit 1
}

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info[:2] != (3, 14):
    raise SystemExit("generate production locks on Ubuntu 26.04 with Python 3.14")
PY

TEMP_DIR="$(mktemp -d)"
readonly TEMP_DIR
cleanup() {
    find "$TEMP_DIR" -mindepth 1 -delete
    rmdir "$TEMP_DIR"
}
trap cleanup EXIT

"$PYTHON_BIN" -m venv "$TEMP_DIR/venv"
"$TEMP_DIR/venv/bin/python" -m pip install --disable-pip-version-check "pip-tools==7.5.0"

for name in production development; do
    "$TEMP_DIR/venv/bin/pip-compile" \
        --allow-unsafe \
        --generate-hashes \
        --no-header \
        --no-emit-index-url \
        --resolver=backtracking \
        --strip-extras \
        --output-file "$REPO_DIR/requirements/$name.lock" \
        "$REPO_DIR/requirements/$name.in"
done

printf 'Generated requirements/production.lock and requirements/development.lock\n'
