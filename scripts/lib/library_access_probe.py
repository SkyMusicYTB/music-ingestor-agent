"""Perform real filesystem operations for post-ACL access verification."""

from __future__ import annotations

import argparse
import os
import secrets
from contextlib import ExitStack
from pathlib import Path

PROBE_PREFIX = ".music-agent-acl-probe-"
PROBE_PAYLOAD = b"music-agent ACL verification\n"


def _open_directory(directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    return os.open(directory, flags)


def verify_read(directory: Path) -> None:
    """Open and enumerate a directory as the current operating-system user."""
    directory_fd = _open_directory(directory)
    try:
        os.listdir(directory_fd)
    finally:
        os.close(directory_fd)


def verify_write(directory: Path) -> None:
    """List a directory and securely create, write, close, and remove a probe file."""
    with ExitStack() as cleanup:
        directory_fd = _open_directory(directory)
        cleanup.callback(os.close, directory_fd)
        os.listdir(directory_fd)

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        probe_name: str | None = None
        probe_fd: int | None = None
        for _ in range(16):
            candidate = f"{PROBE_PREFIX}{secrets.token_hex(16)}.tmp"
            try:
                probe_fd = os.open(candidate, create_flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                continue
            probe_name = candidate
            break
        if probe_name is None or probe_fd is None:
            raise FileExistsError("could not allocate a unique ACL probe file")

        # ExitStack invokes callbacks in reverse: close the file, unlink it, then
        # close the directory. All cleanup callbacks run even if an earlier one fails.
        cleanup.callback(os.unlink, probe_name, dir_fd=directory_fd)
        cleanup.callback(os.close, probe_fd)

        remaining = memoryview(PROBE_PAYLOAD)
        while remaining:
            written = os.write(probe_fd, remaining)
            if written <= 0:
                raise OSError("ACL probe write made no progress")
            remaining = remaining[written:]
        os.fsync(probe_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("read", "write"))
    parser.add_argument("directory", type=Path)
    arguments = parser.parse_args()

    if arguments.operation == "write":
        verify_write(arguments.directory)
    else:
        verify_read(arguments.directory)


if __name__ == "__main__":
    main()
