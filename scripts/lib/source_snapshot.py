"""Create deterministic, symlink-safe content and source-state manifests."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import struct
from pathlib import Path, PurePosixPath

FORMAT_MARKER = b"music-agent-source-snapshot-v1\0"
READ_CHUNK_BYTES = 1024 * 1024


def _open_file_beneath(root_fd: int, relative: PurePosixPath) -> int:
    directory_fd = os.dup(root_fd)
    try:
        for component in relative.parts[:-1]:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            child_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        return os.open(relative.parts[-1], flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def build_snapshots(root: Path, manifest: Path) -> tuple[bytes, bytes]:
    physical_root = root.resolve(strict=True)
    if physical_root != root.absolute() or not physical_root.is_dir():
        raise ValueError("snapshot root must be a physical directory")

    encoded_paths = [value for value in manifest.read_bytes().split(b"\0") if value]
    content = bytearray(FORMAT_MARKER)
    state = bytearray(FORMAT_MARKER)
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    root_fd = os.open(physical_root, root_flags)
    try:
        for encoded in encoded_paths:
            relative = PurePosixPath(os.fsdecode(encoded))
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise ValueError(f"unsafe snapshot path: {encoded!r}")
            file_fd = _open_file_beneath(root_fd, relative)
            try:
                before = os.fstat(file_fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"snapshot entry is not a regular file: {relative}")
                digest = hashlib.sha256()
                while chunk := os.read(file_fd, READ_CHUNK_BYTES):
                    digest.update(chunk)
                after = os.fstat(file_fd)
            finally:
                os.close(file_fd)
            stable_before = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            stable_after = (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if stable_before != stable_after:
                raise RuntimeError(f"source changed while hashing: {relative}")

            path_record = struct.pack(">I", len(encoded)) + encoded
            content_record = path_record + struct.pack(">Q", before.st_size) + digest.digest()
            content.extend(content_record)
            state.extend(
                content_record
                + struct.pack(
                    ">QQIQQQ",
                    before.st_dev,
                    before.st_ino,
                    before.st_mode,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                    before.st_uid,
                )
                + struct.pack(">Q", before.st_gid)
            )
    finally:
        os.close(root_fd)
    return bytes(content), bytes(state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("content_output", type=Path)
    parser.add_argument("state_output", type=Path, nargs="?")
    arguments = parser.parse_args()
    content, state = build_snapshots(arguments.root, arguments.manifest)
    arguments.content_output.write_bytes(content)
    if arguments.state_output is not None:
        arguments.state_output.write_bytes(state)


if __name__ == "__main__":
    main()
