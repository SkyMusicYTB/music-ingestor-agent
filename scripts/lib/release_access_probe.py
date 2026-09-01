"""Validate an installed release from the runtime account's process context."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


def validate_console_scripts(release: Path) -> None:
    bin_dir = release / "venv" / "bin"
    interpreter = bin_dir / "python"

    for entry in bin_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            first_line = entry.open("rb").readline(4096)
        except OSError as exc:
            raise RuntimeError(f"cannot read virtualenv entry point {entry}: {exc}") from exc
        if first_line.startswith(b"#!") and b"/.staging-" in first_line:
            raise RuntimeError(f"console script points to a staging path: {entry}")

    expected = f"#!{interpreter}".encode()
    for name in ("music-agent", "music-agent-worker"):
        first_line = (bin_dir / name).open("rb").readline(4096).rstrip(b"\r\n")
        if first_line != expected:
            raise RuntimeError(f"{name} does not point to the final release interpreter")


def validate_release(release: Path) -> None:
    release = release.resolve(strict=True)
    venv = release / "venv"
    interpreter = venv / "bin" / "python"
    if Path(sys.prefix).resolve(strict=True) != venv.resolve(strict=True):
        raise RuntimeError("release interpreter is not running from the candidate virtualenv")
    if Path(sys.executable).resolve(strict=True) != interpreter.resolve(strict=True):
        raise RuntimeError("release interpreter resolved outside the candidate virtualenv")

    for module in ("app", "fastapi", "sqlalchemy"):
        importlib.import_module(module)
    validate_console_scripts(release)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_access_probe.py RELEASE")
    try:
        validate_release(Path(sys.argv[1]))
    except (OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
