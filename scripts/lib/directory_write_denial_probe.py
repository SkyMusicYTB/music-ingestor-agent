"""Prove that the current account cannot create entries in a directory."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path


def probe(directory: Path, name: str) -> None:
    if not name.startswith(".music-agent-deny-write-") or "/" in name:
        raise ValueError("invalid probe name")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except PermissionError:
        return
    with ExitStack() as cleanup:
        cleanup.callback(os.close, directory_fd)
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(name, create_flags, 0o600, dir_fd=directory_fd)
        except PermissionError:
            return
        cleanup.callback(os.unlink, name, dir_fd=directory_fd)
        cleanup.callback(os.close, file_fd)
        os.write(file_fd, b"music-agent-denial-probe\n")
        os.fsync(file_fd)
    raise PermissionError(f"directory unexpectedly allowed create/write/delete: {directory}")


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: directory_write_denial_probe.py DIRECTORY NAME")
    probe(Path(sys.argv[1]), sys.argv[2])


if __name__ == "__main__":
    main()
