"""Physical-path checks which distinguish absence from inaccessible files."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

Presence = Literal["present", "missing", "unreadable", "unsafe"]


@contextmanager
def open_library_directory(root: Path, relative: str = "") -> Iterator[int | Path]:
    parts = safe_parts(relative) if relative else ()
    if os.name != "posix":
        cursor = root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("symlinked library directory")
        yield cursor
        return
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def safe_parts(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise ValueError("unsafe relative library path")
    if "\\" in relative or "\x00" in relative:
        raise ValueError("unsafe relative library path")
    return path.parts


@contextmanager
def open_library_file(root: Path, relative: str) -> Iterator[BinaryIO]:
    """Open regular audio without following any relative symlink component."""
    parts = safe_parts(relative)
    if os.name != "posix":  # Windows development; production uses descriptor traversal.
        path = root.joinpath(*parts)
        cursor = root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("symlinked library path")
        with path.open("rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("library input is not a regular file")
            yield file
        return
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        audio_fd = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
        )
        with os.fdopen(audio_fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise ValueError("library input is not a regular file")
            yield file
    finally:
        os.close(descriptor)


def library_presence(root: Path, relative: str) -> Presence:
    try:
        with open_library_file(root, relative):
            return "present"
    except FileNotFoundError:
        return "missing"
    except NotADirectoryError:
        # A directory replaced by a symlink must not count as absent.
        cursor = root
        for part in PurePosixPath(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return "unsafe"
        return "missing"
    except ValueError:
        return "unsafe"
    except OSError:
        return "unreadable"
