from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Album, ScanRun, Track
from app.repositories.events import make_event
from app.services.duplicates import normalize_text
from app.services.library_formats import (
    DEFERRED_EXTENSIONS,
    IGNORED_EXTENSIONS,
    PARSER_VERSION,
    SUPPORTED_CODECS,
    SUPPORTED_EXTENSIONS,
    extension_for,
)
from app.services.library_metadata import (
    LibraryReadError,
    _first,
    _number,
    _tag_text,
    _year,
    read_audio_metadata,
    validate_file_snapshot,
)
from app.services.library_presence import (
    library_presence,
    open_library_directory,
    open_library_file,
)

logger = logging.getLogger(__name__)
SCAN_BATCH_SIZE = 200
LEASE_SECONDS = 120


class ScanCancellation(Protocol):
    def is_set(self) -> bool: ...


class ScanAlreadyRunning(RuntimeError):
    def __init__(self, scan_id: str) -> None:
        self.scan_id = scan_id
        super().__init__(f"library scan {scan_id} is already running")


@dataclass
class ScanDiagnostics:
    scan_id: str | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(
            (
                "physical_candidates",
                "indexed",
                "unchanged",
                "newly_indexed",
                "updated",
                "missing",
                "unsupported_extension",
                "unsupported_codec",
                "unreadable",
                "metadata_fallback",
                "skipped_video",
                "errors",
                "ignored",
                "source_aliases",
                "unresolved",
            ),
            0,
        )
    )
    by_extension: dict[str, int] = field(default_factory=dict)
    by_codec: dict[str, int] = field(default_factory=dict)
    samples: list[dict[str, str]] = field(default_factory=list)
    omitted_samples: int = 0
    traversal_complete: bool = True

    def issue(self, relative: str, reason: str) -> None:
        key = {
            "video_bearing": "skipped_video",
            "unsupported_extension": "unsupported_extension",
            "unsupported_codec": "unsupported_codec",
            "unreadable": "unreadable",
            "directory_unreadable": "unreadable",
        }.get(reason, "errors")
        self.counts[key] += 1
        if reason in {
            "unreadable",
            "directory_unreadable",
            "probe_unavailable",
            "probe_resource_limit",
            "file_changed",
        }:
            self.counts["unresolved"] += 1
        if reason == "directory_unreadable":
            self.traversal_complete = False
        safe_path = "".join(c if c.isprintable() else "?" for c in relative)[:300]
        if len(self.samples) < 100:
            self.samples.append({"relative_path": safe_path, "reason_code": reason})
            logger.warning(
                "library file skipped: %s (%s)",
                safe_path,
                reason,
                extra={"scan_id": self.scan_id, "relative_path": safe_path, "reason": reason},
            )
        else:
            self.omitted_samples += 1

    def payload(self, *, details: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "version": 1,
            "parser_version": PARSER_VERSION,
            "counts": self.counts,
            "by_extension": self.by_extension,
            "by_codec": self.by_codec,
            "traversal_complete": self.traversal_complete,
            "coverage_complete": self.traversal_complete and self.counts["unresolved"] == 0,
            "omitted_samples": self.omitted_samples,
        }
        if details:
            result["samples"] = self.samples
        return result


def _raise_if_cancelled(signal: ScanCancellation | None) -> None:
    if signal is not None and signal.is_set():
        raise InterruptedError("library scan interrupted for worker shutdown or lease loss")


def iter_library_candidates(
    root: Path, diagnostics: ScanDiagnostics, cancel_signal: ScanCancellation | None = None
) -> Iterator[Path]:
    root = root.resolve(strict=True)
    stack = [""]
    while stack:
        _raise_if_cancelled(cancel_signal)
        relative_directory = stack.pop()
        try:
            with (
                open_library_directory(root, relative_directory) as directory,
                os.scandir(directory) as entries,
            ):
                for entry in entries:
                    _raise_if_cancelled(cancel_signal)
                    relative = (Path(relative_directory) / entry.name).as_posix()
                    if entry.name.startswith("."):
                        diagnostics.counts["ignored"] += 1
                        continue
                    try:
                        if entry.is_symlink():
                            diagnostics.counts["ignored"] += 1
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(relative)
                        elif entry.is_file(follow_symlinks=False):
                            extension = extension_for(relative)
                            if extension in SUPPORTED_EXTENSIONS:
                                diagnostics.counts["physical_candidates"] += 1
                                diagnostics.by_extension[extension] = (
                                    diagnostics.by_extension.get(extension, 0) + 1
                                )
                                yield root / relative
                            elif extension in IGNORED_EXTENSIONS:
                                diagnostics.counts["ignored"] += 1
                            else:
                                diagnostics.counts["unsupported_extension"] += 1
                                bucket = extension if extension in DEFERRED_EXTENSIONS else "other"
                                diagnostics.by_extension[bucket] = (
                                    diagnostics.by_extension.get(bucket, 0) + 1
                                )
                    except OSError:
                        diagnostics.issue(relative, "unreadable")
        except (OSError, ValueError):
            diagnostics.issue(relative_directory or ".", "directory_unreadable")


def _iter_audio_files(root: Path, cancel_signal: ScanCancellation | None = None) -> Iterator[Path]:
    yield from iter_library_candidates(root, ScanDiagnostics(), cancel_signal)


def scan_has_coverage(scan: ScanRun) -> bool:
    if scan.status != "completed":
        return False
    if scan.parser_version == 0:
        return True
    try:
        payload = json.loads(scan.summary_json)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("coverage_complete") is True


@dataclass(frozen=True)
class _ScanEntry:
    relative: str
    size: int
    mtime_ns: int
    metadata: dict[str, object] | None
    new_record: bool = False


class LibraryScanner:
    def __init__(self, factory: sessionmaker[Session], music_root: Path) -> None:
        self.factory = factory
        self.music_root = music_root

    def _claim(self, full: bool, service_task_id: str | None) -> tuple[str, int, str]:
        now = datetime.now(UTC)
        with self.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            active = session.scalar(select(ScanRun).where(ScanRun.status == "running"))
            if active is not None:
                expires = active.lease_expires_at
                if expires and expires.replace(tzinfo=UTC) > now:
                    raise ScanAlreadyRunning(active.id)
                active.status = "failed"
                active.error_message = "scan interrupted before completion"
                active.completed_at = now
                session.flush()
            generation = int(session.scalar(select(func.max(ScanRun.generation))) or 0) + 1
            initial = any(
                scan_has_coverage(scan)
                for scan in session.scalars(
                    select(ScanRun).where(ScanRun.kind == "initial", ScanRun.status == "completed")
                )
            )
            token = secrets.token_hex(32)
            scan = ScanRun(
                kind=("full" if full else "incremental") if initial else "initial",
                generation=generation,
                status="running",
                parser_version=PARSER_VERSION,
                service_task_id=service_task_id,
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                traversal_complete=False,
            )
            session.add(scan)
            session.flush()
            scan_id = scan.id
            session.commit()
        return scan_id, generation, token

    @staticmethod
    def _fence(session: Session, scan_id: str, token: str) -> ScanRun:
        row = session.scalar(
            select(ScanRun).where(
                ScanRun.id == scan_id,
                ScanRun.lease_token == token,
                ScanRun.status == "running",
                ScanRun.lease_expires_at > datetime.now(UTC),
            )
        )
        if row is None:
            raise InterruptedError("library scan lease lost")
        row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        return row

    def run(
        self,
        full: bool = False,
        *,
        cancel_signal: ScanCancellation | None = None,
        service_task_id: str | None = None,
    ) -> ScanRun:
        _raise_if_cancelled(cancel_signal)
        root = self.music_root.resolve(strict=True)
        scan_id, generation, token = self._claim(full, service_task_id)
        diagnostics = ScanDiagnostics(scan_id=scan_id)
        stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(20):
                try:
                    with self.factory() as session:
                        session.execute(text("BEGIN IMMEDIATE"))
                        self._fence(session, scan_id, token)
                        session.commit()
                except Exception:
                    lease_lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            with self.factory() as session:
                known = {
                    r.filepath: (r.id, r.file_size, r.file_mtime_ns, r.parser_version)
                    for r in session.scalars(select(Track))
                }
            seen: set[str] = set()
            pending: list[_ScanEntry] = []
            for path in iter_library_candidates(root, diagnostics, cancel_signal):
                _raise_if_cancelled(lease_lost)
                relative = path.relative_to(root).as_posix()
                seen.add(relative)
                previous = known.get(relative)
                try:
                    with open_library_file(root, relative) as physical:
                        file_stat = os.fstat(physical.fileno())
                    metadata = None
                    if (
                        not full
                        and previous is not None
                        and previous[1:]
                        == (file_stat.st_size, file_stat.st_mtime_ns, PARSER_VERSION)
                    ):
                        diagnostics.counts["unchanged"] += 1
                    else:
                        metadata = read_audio_metadata(path, music_root=root)
                        if metadata.pop("_metadata_fallback", False):
                            diagnostics.counts["metadata_fallback"] += 1
                    size = (
                        int(str(metadata.get("_file_size", file_stat.st_size)))
                        if metadata
                        else file_stat.st_size
                    )
                    mtime = (
                        int(str(metadata.get("_file_mtime_ns", file_stat.st_mtime_ns)))
                        if metadata
                        else file_stat.st_mtime_ns
                    )
                    pending.append(_ScanEntry(relative, size, mtime, metadata, previous is None))
                except (OSError, ValueError, TypeError) as error:
                    diagnostics.issue(
                        relative,
                        error.reason if isinstance(error, LibraryReadError) else "unreadable",
                    )
                    if previous:
                        pending.append(_ScanEntry(relative, previous[1], previous[2], None))
                if len(pending) >= SCAN_BATCH_SIZE:
                    self._flush_batch(pending, generation, scan_id, token, diagnostics)
                    pending.clear()
            if pending:
                self._flush_batch(pending, generation, scan_id, token, diagnostics)
            _raise_if_cancelled(cancel_signal)
            _raise_if_cancelled(lease_lost)
            for relative, snapshot in known.items():
                if relative in seen:
                    continue
                _raise_if_cancelled(cancel_signal)
                presence = library_presence(root, relative)
                if presence == "missing":
                    with self.factory() as session:
                        session.execute(text("BEGIN IMMEDIATE"))
                        self._fence(session, scan_id, token)
                        result = session.execute(
                            update(Track)
                            .where(
                                Track.id == snapshot[0],
                                Track.filepath == relative,
                                Track.file_size == snapshot[1],
                                Track.file_mtime_ns == snapshot[2],
                                Track.scan_generation < generation,
                                Track.is_present,
                            )
                            .values(is_present=False)
                        )
                        diagnostics.counts["missing"] += int(result.rowcount or 0)
                        session.commit()
                elif presence in {"unreadable", "unsafe"}:
                    diagnostics.issue(relative, presence)
            with self.factory() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                scan = self._fence(session, scan_id, token)
                scan.status = "completed"
                scan.scanned_files = diagnostics.counts["physical_candidates"]
                scan.changed_files = (
                    diagnostics.counts["newly_indexed"] + diagnostics.counts["updated"]
                )
                scan.missing_files = diagnostics.counts["missing"]
                scan.error_count = diagnostics.counts["errors"] + diagnostics.counts["unreadable"]
                scan.traversal_complete = diagnostics.traversal_complete
                scan.summary_json = json.dumps(diagnostics.payload(), separators=(",", ":"))
                scan.completed_at = datetime.now(UTC)
                scan.lease_token = None
                scan.lease_expires_at = None
                session.add(
                    make_event(
                        session,
                        entity_type="library",
                        entity_id=scan.id,
                        event_type="library.scan_completed",
                        message="Library scan completed",
                        details={
                            "scanned": scan.scanned_files,
                            "changed": scan.changed_files,
                            "missing": scan.missing_files,
                            "errors": scan.error_count,
                        },
                        audience="all_authenticated",
                    )
                )
                session.commit()
                session.expunge(scan)
                return scan
        except Exception:
            with self.factory.begin() as session:
                row = session.scalar(
                    select(ScanRun).where(
                        ScanRun.id == scan_id,
                        ScanRun.lease_token == token,
                        ScanRun.status == "running",
                    )
                )
                if row:
                    row.status = "failed"
                    row.error_count = diagnostics.counts["errors"] + 1
                    row.error_message = "library scan interrupted; inspect bounded scan diagnostics"
                    row.summary_json = json.dumps(diagnostics.payload(), separators=(",", ":"))
                    row.completed_at = datetime.now(UTC)
                    row.lease_token = None
                    row.lease_expires_at = None
            raise
        finally:
            stop.set()
            thread.join(timeout=2)

    def index_one(self, path: Path) -> Track:
        root = self.music_root.resolve(strict=True)
        relative = path.relative_to(root).as_posix()
        metadata = read_audio_metadata(path, music_root=root)
        file_stat = validate_file_snapshot(root, relative, metadata)
        with self.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            generation = int(session.scalar(select(func.max(ScanRun.generation))) or 0)
            track, _alias = self._upsert(
                session,
                _ScanEntry(
                    relative,
                    int(str(metadata.get("_file_size", file_stat.st_size))),
                    int(str(metadata.get("_file_mtime_ns", file_stat.st_mtime_ns))),
                    metadata,
                ),
                generation,
            )
            session.commit()
            assert track is not None
            session.expunge(track)
            return track

    def _flush_batch(
        self,
        entries: list[_ScanEntry],
        generation: int,
        scan_id: str,
        token: str,
        diagnostics: ScanDiagnostics,
    ) -> None:
        root = self.music_root.resolve(strict=True)
        validated = []
        for entry in entries:
            if entry.metadata is not None:
                try:
                    validate_file_snapshot(root, entry.relative, entry.metadata)
                except LibraryReadError as error:
                    diagnostics.issue(entry.relative, error.reason)
                    continue
            validated.append(entry)
        with self.factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            scan = self._fence(session, scan_id, token)
            for entry in validated:
                track, alias = self._upsert(session, entry, generation)
                if track:
                    if entry.metadata is not None:
                        diagnostics.counts["newly_indexed" if entry.new_record else "updated"] += 1
                    diagnostics.counts["indexed"] += 1
                    codec = (track.codec or "unknown")[:64]
                    if codec not in SUPPORTED_CODECS:
                        codec = "other"
                    diagnostics.by_codec[codec] = diagnostics.by_codec.get(codec, 0) + 1
                    diagnostics.counts["source_aliases"] += int(alias)
            scan.scanned_files = diagnostics.counts["physical_candidates"]
            scan.changed_files = diagnostics.counts["newly_indexed"] + diagnostics.counts["updated"]
            scan.summary_json = json.dumps(diagnostics.payload(), separators=(",", ":"))
            session.commit()

    def _upsert(
        self, session: Session, entry: _ScanEntry, generation: int
    ) -> tuple[Track | None, bool]:
        track = session.scalar(select(Track).where(Track.filepath == entry.relative))
        if entry.metadata is None:
            if track:
                track.is_present = True
                track.scan_generation = generation
            return track, False
        metadata = {
            key: value
            for key, value in entry.metadata.items()
            if not key.startswith("_") and key != "job_id"
        }
        try:
            provenance = json.loads(track.provenance_json) if track else {}
        except ValueError:
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        # A changed file must not retain a previous source's deduplication alias.
        # Reconstruct it only from provenance actually read from the current file.
        provenance.pop("source_alias", None)
        job_id = entry.metadata.get("job_id")
        if isinstance(job_id, str) and job_id:
            provenance["job_id"] = job_id
        extractor, source_id = metadata.get("source_extractor"), metadata.get("source_id")
        alias = False
        if extractor and source_id:
            owner = session.scalar(
                select(Track).where(
                    Track.source_extractor == extractor,
                    Track.source_id == source_id,
                    Track.filepath != entry.relative,
                )
            )
            if owner:
                provenance["source_alias"] = {
                    "extractor": extractor,
                    "id": source_id,
                    "url": metadata.get("source_url"),
                    "owner_track_id": owner.id,
                }
                metadata["source_extractor"] = metadata["source_id"] = None
                alias = True
        values = {
            **metadata,
            "album_id": self._album_id(session, metadata),
            "artist_normalized": normalize_text(str(metadata["artist"])),
            "title_normalized": normalize_text(str(metadata["title"])),
            "filepath": entry.relative,
            "file_extension": extension_for(entry.relative),
            "is_present": True,
            "file_mtime_ns": entry.mtime_ns,
            "file_size": entry.size,
            "scan_generation": generation,
            "parser_version": PARSER_VERSION,
            "provenance_json": json.dumps(provenance, separators=(",", ":")),
        }
        if track is None:
            track = Track(**values)
            session.add(track)
        else:
            for key, value in values.items():
                setattr(track, key, value)
        session.flush()
        return track, alias

    @staticmethod
    def _album_id(session: Session, metadata: dict[str, object]) -> str | None:
        album = metadata.get("album")
        if not album:
            return None
        artist = str(metadata.get("album_artist") or metadata["artist"])
        year = metadata.get("year")
        release_mbid = metadata.get("release_mbid")
        identity = (
            f"mbid:{release_mbid}"
            if release_mbid
            else f"text:{normalize_text(artist)}:{normalize_text(str(album))}:{year or ''}"
        )
        existing = session.scalar(select(Album).where(Album.identity_key == identity))
        if existing is None:
            existing = Album(
                identity_key=identity,
                artist=artist,
                artist_normalized=normalize_text(artist),
                title=str(album),
                title_normalized=normalize_text(str(album)),
                year=year if isinstance(year, int) else None,
                release_mbid=str(release_mbid) if release_mbid else None,
                release_group_mbid=str(metadata["release_group_mbid"])
                if metadata.get("release_group_mbid")
                else None,
            )
            session.add(existing)
            session.flush()
        return existing.id


__all__ = [
    "LibraryScanner",
    "ScanAlreadyRunning",
    "ScanDiagnostics",
    "_first",
    "_number",
    "_tag_text",
    "_year",
    "iter_library_candidates",
    "read_audio_metadata",
    "scan_has_coverage",
]
