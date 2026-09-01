from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import stat
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_UNSAFE_COMPONENT_RE = re.compile(r"[\x00-\x1f\x7f/\\:]+")
_MULTISPACE_RE = re.compile(r"\s+")
_SAFE_JOB_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_WINDOWS_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)
_COPY_CHUNK = 1024 * 1024
MAX_RELATIVE_PATH_BYTES = 1000


class UnsafePathError(ValueError):
    pass


class DestinationExistsError(FileExistsError):
    pass


class InsufficientSpaceError(OSError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationResult:
    path: Path
    relative_path: str
    sha256: str
    size: int


def safe_component(value: str, *, fallback: str = "Unknown", max_bytes: int = 180) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _UNSAFE_COMPONENT_RE.sub("_", normalized)
    normalized = _MULTISPACE_RE.sub(" ", normalized).strip(" .")
    if normalized in {"", ".", ".."}:
        normalized = fallback
    while len(normalized.encode("utf-8")) > max_bytes:
        normalized = normalized[:-1]
    normalized = normalized.strip(" .")
    if _WINDOWS_RESERVED_RE.fullmatch(normalized):
        normalized = f"_{normalized}"
    return normalized or fallback


def build_track_relative_path(
    *,
    artist: str,
    title: str,
    album: str | None,
    track_number: int | None,
    extension: str,
    year: int | None = None,
    disc_number: int | None = None,
    disc_total: int | None = None,
    source_id: str | None = None,
    include_source_id: bool = False,
) -> str:
    clean_extension = extension.lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{2,5}", clean_extension):
        raise UnsafePathError("media extension is not allowed")
    artist_component = safe_component(artist)
    album_label = album or "Singles"
    if year is not None and 1800 <= year <= 2200 and not album_label.rstrip().endswith(f"({year})"):
        album_label = f"{album_label} ({year})"
    album_component = safe_component(album_label)
    prefix = f"{track_number:02d} - " if track_number is not None and track_number > 0 else ""
    suffix = ""
    if include_source_id and source_id:
        suffix = f" [{safe_component(source_id, fallback='source', max_bytes=48)}]"
    filename = safe_component(f"{prefix}{title}{suffix}", max_bytes=220) + f".{clean_extension}"
    parts = [artist_component, album_component]
    if (
        disc_number is not None
        and disc_number > 0
        and (disc_number > 1 or (disc_total is not None and disc_total > 1))
    ):
        parts.append(f"Disc {disc_number:02d}")
    relative = PurePosixPath(*parts, filename).as_posix()
    validate_relative_path(relative)
    return relative


def add_source_collision_suffix(relative_path: str, source_id: str) -> str:
    relative = validate_relative_path(relative_path)
    suffix = safe_component(source_id, fallback="source", max_bytes=48)
    decoration = f" [{suffix}]"
    remaining_stem_bytes = 255 - len(f"{decoration}{relative.suffix}".encode())
    if remaining_stem_bytes < 1:
        raise UnsafePathError("source collision suffix leaves no room for a filename")
    stem = safe_component(relative.stem, fallback="Track", max_bytes=remaining_stem_bytes)
    filename = f"{stem}{decoration}{relative.suffix}"
    result = PurePosixPath(*relative.parts[:-1], filename)
    validate_relative_path(result)
    return result.as_posix()


def validate_relative_path(value: str | PurePosixPath) -> PurePosixPath:
    raw = str(value)
    if not raw or "\x00" in raw or "\\" in raw:
        raise UnsafePathError("destination path is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("/"):
        raise UnsafePathError("destination path must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError("destination path contains traversal")
    for part in path.parts:
        if len(part.encode("utf-8")) > 255:
            raise UnsafePathError("destination path component is too long")
        if any(ord(character) < 32 or ord(character) == 127 for character in part):
            raise UnsafePathError("destination path contains control characters")
    if len(path.parts) < 1 or not path.name:
        raise UnsafePathError("destination path must name a file")
    if len(path.as_posix().encode("utf-8")) > MAX_RELATIVE_PATH_BYTES:
        raise UnsafePathError("destination path exceeds the total path limit")
    return path


def ensure_free_space(path: Path, *, required_bytes: int, reserve_bytes: int = 0) -> None:
    if required_bytes < 0 or reserve_bytes < 0:
        raise ValueError("space requirements cannot be negative")
    free = shutil.disk_usage(path).free
    if free < required_bytes + reserve_bytes:
        raise InsufficientSpaceError(
            errno.ENOSPC,
            f"insufficient free space: need {required_bytes + reserve_bytes}, have {free}",
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise UnsafePathError("hash target is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(_COPY_CHUNK), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def create_staging_directory(root: Path, job_id: str) -> Path:
    if not _SAFE_JOB_ID_RE.fullmatch(job_id):
        raise UnsafePathError("job ID is not safe for a staging directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise UnsafePathError("staging root must be a real directory")
    root = root.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, flags)
    try:
        created = False
        try:
            os.mkdir(job_id, mode=0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            pass
        directory_fd = os.open(
            job_id,
            flags,
            dir_fd=root_fd,
        )
        try:
            directory_stat = os.fstat(directory_fd)
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise UnsafePathError("staging path is not a directory")
            if directory_stat.st_uid != os.getuid():
                raise UnsafePathError("staging directory has an unexpected owner")
            if stat.S_IMODE(directory_stat.st_mode) != 0o700:
                raise UnsafePathError("staging directory permissions must be 0700")
        finally:
            os.close(directory_fd)
        if created:
            os.fsync(root_fd)
    finally:
        os.close(root_fd)
    result = root / job_id
    if result.is_symlink() or not result.is_dir():  # defense against a concurrent replacement
        raise UnsafePathError("staging directory was replaced unexpectedly")
    return result


@contextmanager
def _open_destination_directory(root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise UnsafePathError("library root must be a real directory")
    root = root.resolve(strict=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(root, flags)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise UnsafePathError("destination contains a symlink or non-directory") from exc
            os.close(current_fd)
            current_fd = next_fd
        yield current_fd
    finally:
        os.close(current_fd)


def publish_no_clobber(
    source: Path,
    music_root: Path,
    relative_path: str | PurePosixPath,
    *,
    reserve_bytes: int = 0,
    remove_source: bool = False,
) -> PublicationResult:
    """Copy, fsync, and atomically link a complete file into the library.

    The temporary inode is created in the destination directory. A hard-link is
    then used as the no-replace atomic publish primitive, so an existing path is
    never overwritten even when another worker wins the race.
    """

    relative = validate_relative_path(relative_path)
    source_stat = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
        raise UnsafePathError("publication source must be a regular non-symlink file")
    music_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(music_root, required_bytes=source_stat.st_size, reserve_bytes=reserve_bytes)
    temp_name = f".{relative.name}.partial-{uuid.uuid4().hex}"
    destination = music_root.resolve(strict=True).joinpath(*relative.parts)
    digest = hashlib.sha256()
    copied = 0

    with _open_destination_directory(music_root, relative.parts[:-1]) as directory_fd:
        output_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        output_flags |= getattr(os, "O_NOFOLLOW", 0)
        input_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        input_flags |= getattr(os, "O_NOFOLLOW", 0)
        output_fd = os.open(temp_name, output_flags, 0o644, dir_fd=directory_fd)
        input_fd: int | None = None
        published = False
        try:
            input_fd = os.open(source, input_flags)
            opened_stat = os.fstat(input_fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise UnsafePathError("publication source changed type while opening")
            if (opened_stat.st_dev, opened_stat.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                raise UnsafePathError("publication source changed while opening")
            with (
                os.fdopen(input_fd, "rb", closefd=False) as input_stream,
                os.fdopen(output_fd, "wb", closefd=False) as output_stream,
            ):
                for chunk in iter(lambda: input_stream.read(_COPY_CHUNK), b""):
                    output_stream.write(chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                output_stream.flush()
            os.fsync(output_fd)
            final_input_stat = os.fstat(input_fd)
            if (
                copied != source_stat.st_size
                or final_input_stat.st_size != source_stat.st_size
                or final_input_stat.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise UnsafePathError("publication source changed while copying")
            try:
                os.link(
                    temp_name,
                    relative.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise DestinationExistsError(f"destination already exists: {relative}") from exc
            published = True
            os.fsync(directory_fd)
        finally:
            if input_fd is not None:
                os.close(input_fd)
            os.close(output_fd)
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            if not published:
                os.fsync(directory_fd)

    if remove_source:
        source.unlink()
    return PublicationResult(
        path=destination,
        relative_path=relative.as_posix(),
        sha256=digest.hexdigest(),
        size=copied,
    )


def publish_album_cover_no_clobber(
    data: bytes,
    music_root: Path,
    album_relative_directory: str | PurePosixPath,
    *,
    reserve_bytes: int = 0,
) -> PublicationResult:
    """Atomically create `cover.jpg` and never replace an existing sidecar."""

    if not data:
        raise ValueError("cover artwork cannot be empty")
    directory = PurePosixPath(str(album_relative_directory))
    relative = validate_relative_path(directory / "cover.jpg")
    music_root.mkdir(parents=True, exist_ok=True)
    ensure_free_space(music_root, required_bytes=len(data), reserve_bytes=reserve_bytes)
    digest = hashlib.sha256(data).hexdigest()
    temp_name = f".cover.jpg.partial-{uuid.uuid4().hex}"
    destination = music_root.resolve(strict=True).joinpath(*relative.parts)
    with _open_destination_directory(music_root, relative.parts[:-1]) as directory_fd:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp_name, flags, 0o644, dir_fd=directory_fd)
        published = False
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
            os.fsync(descriptor)
            try:
                os.link(
                    temp_name,
                    "cover.jpg",
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise DestinationExistsError(f"destination already exists: {relative}") from exc
            published = True
            os.fsync(directory_fd)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            if not published:
                os.fsync(directory_fd)
    return PublicationResult(
        path=destination,
        relative_path=relative.as_posix(),
        sha256=digest,
        size=len(data),
    )
