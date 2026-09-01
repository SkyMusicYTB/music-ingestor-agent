from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from mutagen import File as MutagenFile  # type: ignore[attr-defined]
from mutagen import MutagenError  # type: ignore[attr-defined]
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Album, Event, ScanRun, Track
from app.services.duplicates import normalize_text, version_signature

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus"}
SCAN_BATCH_SIZE = 200


class ScanCancellation(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ScanEntry:
    relative: str
    size: int
    mtime_ns: int
    metadata: dict[str, object] | None


def _first(tags: Any, *keys: str) -> str | None:
    if tags is None:
        return None
    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError):
            continue
        if value is None:
            continue
        converted = _tag_text(value)
        if converted is not None:
            return converted
    return None


def _tag_text(value: object) -> str | None:
    if hasattr(value, "text"):
        return _tag_text(value.text)
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, list | tuple):
        if not value:
            return None
        if (
            len(value) >= 2
            and isinstance(value[0], int)
            and not isinstance(value[0], bool)
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
        ):
            return f"{value[0]}/{value[1]}" if value[1] > 0 else str(value[0])
        return _tag_text(value[0])
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _number(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    pieces = value.split("/", 1)
    try:
        number = int(pieces[0])
    except (ValueError, TypeError):
        return None, None
    try:
        total = int(pieces[1]) if len(pieces) == 2 else None
    except (ValueError, TypeError):
        total = None
    return number, total


def _year(value: str | None) -> int | None:
    if not value:
        return None
    try:
        result = int(value[:4])
    except ValueError:
        return None
    return result if 1800 <= result <= 2200 else None


def _raise_if_cancelled(signal: ScanCancellation | None) -> None:
    if signal is not None and signal.is_set():
        raise InterruptedError("library scan was cancelled for worker shutdown")


def _iter_audio_files(root: Path, cancel_signal: ScanCancellation | None = None) -> Iterator[Path]:
    root = root.resolve(strict=True)
    stack = [root]
    while stack:
        _raise_if_cancelled(cancel_signal)
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    _raise_if_cancelled(cancel_signal)
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif (
                            entry.is_file(follow_symlinks=False)
                            and Path(entry.name).suffix.casefold() in SUPPORTED_EXTENSIONS
                        ):
                            yield Path(entry.path)
                    except OSError:
                        logger.warning("unable to stat library entry", extra={"path": entry.path})
        except OSError:
            logger.exception("unable to scan library directory", extra={"path": str(directory)})


def _raw_tag(tags: Any, key: str) -> str | None:
    if tags is None:
        return None
    # Vorbis comments
    result = _first(tags, key, key.upper(), key.lower())
    if result:
        return result
    # ID3 TXXX frames
    try:
        frames = tags.getall("TXXX")
    except AttributeError:
        frames = []
    for frame in frames:
        if getattr(frame, "desc", "").casefold() == key.casefold():
            text = getattr(frame, "text", [])
            if text:
                return str(text[0])
    # MP4 freeform atoms
    return _first(tags, f"----:com.apple.iTunes:{key.upper()}")


def read_audio_metadata(path: Path) -> dict[str, object]:
    audio = MutagenFile(path, easy=False)
    if audio is None or getattr(audio, "info", None) is None:
        raise ValueError("unsupported or unreadable audio")
    tags = getattr(audio, "tags", None)
    easy = MutagenFile(path, easy=True)
    easy_tags = getattr(easy, "tags", None)
    artist = _first(easy_tags, "artist") or _first(tags, "TPE1", "\xa9ART") or "Unknown Artist"
    title = _first(easy_tags, "title") or _first(tags, "TIT2", "\xa9nam") or path.stem
    album = _first(easy_tags, "album") or _first(tags, "TALB", "\xa9alb")
    album_artist = _first(easy_tags, "albumartist") or _first(tags, "TPE2", "aART")
    date = _first(easy_tags, "date") or _first(tags, "TDRC", "\xa9day")
    genre = _first(easy_tags, "genre") or _first(tags, "TCON", "\xa9gen")
    track_number, track_total = _number(
        _first(easy_tags, "tracknumber") or _first(tags, "TRCK", "trkn")
    )
    disc_number, disc_total = _number(
        _first(easy_tags, "discnumber") or _first(tags, "TPOS", "disk")
    )
    codec = type(audio).__name__.casefold()
    bitrate = getattr(audio.info, "bitrate", None)
    return {
        "artist": artist,
        "title": title,
        "album": album,
        "album_artist": album_artist,
        "genre": genre,
        "year": _year(date),
        "track_number": track_number,
        "track_total": track_total,
        "disc_number": disc_number,
        "disc_total": disc_total,
        "duration_seconds": float(audio.info.length),
        "codec": codec,
        "bitrate": int(bitrate) if bitrate else None,
        "recording_mbid": _raw_tag(tags, "MUSICBRAINZ_TRACKID"),
        "release_mbid": _raw_tag(tags, "MUSICBRAINZ_ALBUMID"),
        "release_group_mbid": _raw_tag(tags, "MUSICBRAINZ_RELEASEGROUPID"),
        "source_extractor": _raw_tag(tags, "MUSIC_AGENT_SOURCE_EXTRACTOR"),
        "source_id": _raw_tag(tags, "MUSIC_AGENT_SOURCE_ID"),
        "source_url": _raw_tag(tags, "MUSIC_AGENT_SOURCE_URL"),
        "job_id": _raw_tag(tags, "MUSIC_AGENT_JOB_ID"),
        "version_signature": version_signature(title, album),
    }


class LibraryScanner:
    def __init__(self, factory: sessionmaker[Session], music_root: Path) -> None:
        self.factory = factory
        self.music_root = music_root

    def run(self, full: bool = False, *, cancel_signal: ScanCancellation | None = None) -> ScanRun:
        _raise_if_cancelled(cancel_signal)
        self.music_root.mkdir(parents=True, exist_ok=True)
        root = self.music_root.resolve(strict=True)
        with self.factory.begin() as session:
            generation = int(session.scalar(select(func.max(ScanRun.generation))) or 0) + 1
            has_initial = session.scalar(
                select(ScanRun.id).where(ScanRun.kind == "initial", ScanRun.status == "completed")
            )
            scan = ScanRun(
                kind="initial" if has_initial is None else ("full" if full else "incremental"),
                generation=generation,
                status="running",
            )
            session.add(scan)
            session.flush()
            scan_id = scan.id
        scanned = changed = errors = 0
        album_cache: dict[str, str] = {}
        with self.factory() as session:
            known = {
                row.filepath: (row.file_size, row.file_mtime_ns)
                for row in session.scalars(select(Track))
            }
        pending: list[_ScanEntry] = []
        try:
            for path in _iter_audio_files(root, cancel_signal):
                _raise_if_cancelled(cancel_signal)
                scanned += 1
                relative = PurePosixPath(path.relative_to(root).as_posix()).as_posix()
                stat_result = path.stat(follow_symlinks=False)
                previous = known.get(relative)
                metadata: dict[str, object] | None = None
                if full or previous != (stat_result.st_size, stat_result.st_mtime_ns):
                    try:
                        metadata = read_audio_metadata(path)
                    except (MutagenError, OSError, ValueError, TypeError):
                        errors += 1
                        logger.exception("failed to read audio tags", extra={"path": relative})
                        # A previously indexed but now unreadable file is still present.
                        if previous is None:
                            continue
                    else:
                        changed += 1
                pending.append(
                    _ScanEntry(
                        relative=relative,
                        size=stat_result.st_size,
                        mtime_ns=stat_result.st_mtime_ns,
                        metadata=metadata,
                    )
                )
                if len(pending) >= SCAN_BATCH_SIZE:
                    self._flush_batch(pending, generation, album_cache)
                    pending.clear()
            _raise_if_cancelled(cancel_signal)
            if pending:
                self._flush_batch(pending, generation, album_cache)
            _raise_if_cancelled(cancel_signal)
            with self.factory.begin() as session:
                missing_result = session.execute(
                    update(Track)
                    .where(Track.is_present, Track.scan_generation != generation)
                    .values(is_present=False)
                )
                missing = int(missing_result.rowcount or 0)
                completed_scan = session.get(ScanRun, scan_id)
                assert completed_scan is not None
                completed_scan.status = "completed"
                completed_scan.scanned_files = scanned
                completed_scan.changed_files = changed
                completed_scan.missing_files = missing
                completed_scan.error_count = errors
                completed_scan.completed_at = datetime.now(UTC)
                session.add(
                    Event(
                        entity_type="library",
                        entity_id=completed_scan.id,
                        event_type="library.scan_completed",
                        message=f"Library scan completed: {scanned} files",
                        details_json=(
                            f'{{"scanned":{scanned},"changed":{changed},"missing":{missing},'
                            f'"errors":{errors}}}'
                        ),
                    )
                )
                session.flush()
                session.expunge(completed_scan)
                return completed_scan
        except Exception as error:
            with self.factory.begin() as session:
                failed_scan = session.get(ScanRun, scan_id)
                if failed_scan:
                    failed_scan.status = "failed"
                    failed_scan.scanned_files = scanned
                    failed_scan.changed_files = changed
                    failed_scan.error_count = errors + 1
                    failed_scan.error_message = str(error)[:1000]
                    failed_scan.completed_at = datetime.now(UTC)
            raise

    def index_one(self, path: Path) -> Track:
        root = self.music_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise ValueError("published path escapes music root")
        relative = resolved.relative_to(root).as_posix()
        stat_result = resolved.stat(follow_symlinks=False)
        metadata = read_audio_metadata(resolved)
        with self.factory.begin() as session:
            generation = int(session.scalar(select(func.max(ScanRun.generation))) or 0)
            album_id = self._album_id(session, metadata, {})
            values = self._track_values(
                relative,
                stat_result.st_size,
                stat_result.st_mtime_ns,
                metadata,
                album_id,
                generation,
            )
            track = session.scalar(select(Track).where(Track.filepath == relative))
            if track is None:
                track = Track(**values)
                session.add(track)
            else:
                for key, value in values.items():
                    setattr(track, key, value)
            session.flush()
            session.expunge(track)
            return track

    def _flush_batch(
        self,
        entries: list[_ScanEntry],
        generation: int,
        album_cache: dict[str, str],
    ) -> None:
        paths = [entry.relative for entry in entries]
        with self.factory.begin() as session:
            existing = {
                track.filepath: track
                for track in session.scalars(select(Track).where(Track.filepath.in_(paths)))
            }
            for entry in entries:
                track = existing.get(entry.relative)
                if entry.metadata is None:
                    if track is not None:
                        track.is_present = True
                        track.scan_generation = generation
                    continue
                album_id = self._album_id(session, entry.metadata, album_cache)
                values = self._track_values(
                    entry.relative,
                    entry.size,
                    entry.mtime_ns,
                    entry.metadata,
                    album_id,
                    generation,
                )
                if track is None:
                    session.add(Track(**values))
                else:
                    for key, value in values.items():
                        setattr(track, key, value)

    @staticmethod
    def _track_values(
        relative: str,
        size: int,
        mtime_ns: int,
        metadata: dict[str, object],
        album_id: str | None,
        generation: int,
    ) -> dict[str, object]:
        track_metadata = {key: value for key, value in metadata.items() if key != "job_id"}
        values: dict[str, object] = {
            **track_metadata,
            "album_id": album_id,
            "artist_normalized": normalize_text(str(metadata["artist"])),
            "title_normalized": normalize_text(str(metadata["title"])),
            "filepath": relative,
            "is_present": True,
            "file_mtime_ns": mtime_ns,
            "file_size": size,
            "scan_generation": generation,
        }
        job_id = metadata.get("job_id")
        if isinstance(job_id, str) and job_id:
            values["provenance_json"] = json.dumps(
                {"job_id": job_id}, ensure_ascii=False, separators=(",", ":")
            )
        return values

    @staticmethod
    def _album_id(
        session: Session, metadata: dict[str, object], cache: dict[str, str]
    ) -> str | None:
        album = metadata.get("album")
        if not album:
            return None
        artist = str(metadata.get("album_artist") or metadata["artist"])
        year = metadata.get("year")
        numeric_year = year if isinstance(year, int) and not isinstance(year, bool) else None
        release_mbid = metadata.get("release_mbid")
        identity = (
            f"mbid:{release_mbid}"
            if release_mbid
            else f"text:{normalize_text(artist)}:{normalize_text(str(album))}:{numeric_year or ''}"
        )
        if identity in cache:
            return cache[identity]
        existing = session.scalar(select(Album).where(Album.identity_key == identity))
        if existing is None:
            existing = Album(
                identity_key=identity,
                artist=artist,
                artist_normalized=normalize_text(artist),
                title=str(album),
                title_normalized=normalize_text(str(album)),
                year=numeric_year,
                release_mbid=str(release_mbid) if release_mbid else None,
                release_group_mbid=(
                    str(metadata["release_group_mbid"])
                    if metadata.get("release_group_mbid")
                    else None
                ),
            )
            session.add(existing)
            session.flush()
        cache[identity] = existing.id
        return existing.id
