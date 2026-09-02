"""Perform real filesystem operations for post-ACL access verification."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from contextlib import ExitStack
from pathlib import Path

PROBE_PREFIX = ".music-agent-acl-probe-"
PROBE_PAYLOAD = b"music-agent ACL verification\n"
PROBE_FILE = "access-check"
MAX_READ_DEPTH = 16


def _open_directory(directory: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    return os.open(directory, flags)


def _read_first_regular_file(directory_fd: int, *, depth: int = 0) -> bool:
    """Open and read from one existing physical file beneath a directory."""
    if depth >= MAX_READ_DEPTH:
        return False
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                os.read(file_fd, 1)
            finally:
                os.close(file_fd)
            return True
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        child_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            if _read_first_regular_file(child_fd, depth=depth + 1):
                return True
        finally:
            os.close(child_fd)
    return False


def verify_read(directory: Path) -> None:
    """List the tree and read one physical file when the library is nonempty."""
    directory_fd = _open_directory(directory)
    try:
        os.listdir(directory_fd)
        _read_first_regular_file(directory_fd)
    finally:
        os.close(directory_fd)


def verify_write(directory: Path) -> None:
    """Create a nested probe so access and inherited default ACLs are exercised."""
    with ExitStack() as cleanup:
        directory_fd = _open_directory(directory)
        cleanup.callback(os.close, directory_fd)
        os.listdir(directory_fd)

        probe_name: str | None = None
        for _ in range(16):
            candidate = f"{PROBE_PREFIX}{secrets.token_hex(16)}.tmp"
            try:
                os.mkdir(candidate, 0o770, dir_fd=directory_fd)
            except FileExistsError:
                continue
            probe_name = candidate
            break
        if probe_name is None:
            raise FileExistsError("could not allocate a unique ACL probe directory")

        # ExitStack invokes callbacks in reverse: close and unlink the file, close
        # and remove the child directory, then close the library directory. Every
        # cleanup callback runs even when verification raises.
        cleanup.callback(os.rmdir, probe_name, dir_fd=directory_fd)
        child_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        child_fd = os.open(probe_name, child_flags, dir_fd=directory_fd)
        cleanup.callback(os.close, child_fd)

        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        probe_fd = os.open(PROBE_FILE, create_flags, 0o660, dir_fd=child_fd)
        cleanup.callback(os.unlink, PROBE_FILE, dir_fd=child_fd)
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
