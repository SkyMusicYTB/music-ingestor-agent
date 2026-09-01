#!/usr/bin/env python3
"""Consistent SQLite backup, verification, and atomic restore using stdlib APIs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

BUFFER_SIZE = 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def inspect(connection: sqlite3.Connection) -> dict[str, object]:
    integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        raise RuntimeError(f"SQLite integrity check failed: {integrity_rows!r}")
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if journal_mode != "delete":
        raise RuntimeError(f"unsafe SQLite journal mode {journal_mode!r}; expected 'delete'")
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone()
    revision = None
    if table_exists:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else None
    return {
        "integrity": "ok",
        "journal_mode": journal_mode,
        "foreign_keys": foreign_keys,
        "schema_revision": revision,
        "sqlite_version": sqlite3.sqlite_version,
    }


def fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_atomic(path: Path, payload: bytes, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def copy_database(source: Path, destination: Path, *, replace: bool) -> dict[str, object]:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not replace:
        raise FileExistsError(f"refusing to overwrite backup: {destination}")
    if replace:
        sidecars = [
            Path(f"{destination}{suffix}")
            for suffix in ("-journal", "-wal", "-shm")
            if Path(f"{destination}{suffix}").exists()
        ]
        if sidecars:
            raise RuntimeError(f"refusing restore while SQLite sidecars exist: {sidecars!r}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = connect_read_only(source)
        source_metadata = inspect(source_connection)
        destination_connection = sqlite3.connect(temporary, timeout=10)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        destination_connection.execute("PRAGMA synchronous=FULL")
        destination_connection.execute("PRAGMA foreign_keys=ON")
        source_connection.backup(destination_connection, pages=256, sleep=0.05)
        destination_connection.commit()
        destination_metadata = inspect(destination_connection)
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        os.chmod(temporary, 0o600)
        fsync_file(temporary)
        if replace:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)
            temporary.unlink()
        fsync_directory(destination.parent)
        return {"source": source_metadata, "destination": destination_metadata}
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        temporary.unlink(missing_ok=True)


def backup(source: Path, destination: Path, label: str) -> dict[str, object]:
    related_paths = (
        destination,
        destination.with_suffix(destination.suffix + ".sha256"),
        destination.with_suffix(destination.suffix + ".json"),
    )
    existing = [str(path) for path in related_paths if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite backup artifacts: {existing!r}")
    metadata = copy_database(source, destination, replace=False)
    digest = sha256(destination)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    manifest = {
        "format": 1,
        "kind": "music-agent-sqlite-backup",
        "created_at": created_at,
        "label": label,
        "source": str(source),
        "backup": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": digest,
        **metadata["destination"],
    }
    try:
        write_atomic(
            destination.with_suffix(destination.suffix + ".sha256"),
            f"{digest}  {destination.name}\n".encode(),
        )
        write_atomic(
            destination.with_suffix(destination.suffix + ".json"),
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        )
    except BaseException:
        for path in related_paths:
            path.unlink(missing_ok=True)
        fsync_directory(destination.parent)
        raise
    return manifest


def expected_digest(path: Path) -> str | None:
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    if not checksum_path.is_file():
        return None
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise RuntimeError(f"invalid checksum file: {checksum_path}")
    first_line = lines[0]
    digest = first_line.split()[0]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RuntimeError(f"invalid checksum file: {checksum_path}")
    return digest


def verify(path: Path, require_checksum: bool) -> dict[str, object]:
    expected = expected_digest(path)
    if require_checksum and expected is None:
        raise RuntimeError(f"checksum sidecar is required for {path}")
    actual = sha256(path)
    if expected is not None and actual != expected:
        raise RuntimeError(f"checksum mismatch for {path}")
    connection = connect_read_only(path)
    try:
        metadata = inspect(connection)
    finally:
        connection.close()
    manifest_path = path.with_suffix(path.suffix + ".json")
    if require_checksum and not manifest_path.is_file():
        raise RuntimeError(f"backup manifest is required for {path}")
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(f"invalid backup manifest: {manifest_path}") from error
        if not isinstance(manifest, dict):
            raise RuntimeError(f"invalid backup manifest: {manifest_path}")
        expected_fields = {
            "format": 1,
            "kind": "music-agent-sqlite-backup",
            "bytes": path.stat().st_size,
            "sha256": actual,
            "integrity": metadata["integrity"],
            "journal_mode": metadata["journal_mode"],
            "schema_revision": metadata["schema_revision"],
        }
        mismatched = {
            key: {"expected": value, "found": manifest.get(key)}
            for key, value in expected_fields.items()
            if manifest.get(key) != value
        }
        if mismatched:
            raise RuntimeError(f"backup manifest mismatch: {mismatched!r}")
    return {"path": str(path), "sha256": actual, **metadata}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", type=Path, required=True)
    backup_parser.add_argument("--destination", type=Path, required=True)
    backup_parser.add_argument("--label", default="manual")

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("path", type=Path)
    verify_parser.add_argument("--require-checksum", action="store_true")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--source", type=Path, required=True)
    restore_parser.add_argument("--destination", type=Path, required=True)
    restore_parser.add_argument("--require-checksum", action="store_true")
    return parser


def main() -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args()
    if arguments.command == "backup":
        result = backup(arguments.source, arguments.destination, arguments.label)
    elif arguments.command == "verify":
        result = verify(arguments.path, arguments.require_checksum)
    else:
        verify(arguments.source, arguments.require_checksum)
        metadata = copy_database(arguments.source, arguments.destination, replace=True)
        result = {"restored": str(arguments.destination), **metadata["destination"]}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, sqlite3.Error) as error:
        print(f"sqlite-maintenance: {error}", file=sys.stderr)
        raise SystemExit(1) from error
