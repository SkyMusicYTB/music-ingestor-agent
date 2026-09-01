from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "sqlite-maintenance.py"


def run_helper(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository helper
        [sys.executable, str(HELPER), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def create_database(path: Path, value: str = "original") -> None:
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0001')")
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def test_backup_is_consistent_checksummed_and_manifested(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "backups" / "snapshot.db"
    create_database(source)

    result = run_helper(
        "backup",
        "--source",
        str(source),
        "--destination",
        str(backup),
        "--label",
        "test",
    )
    output = json.loads(result.stdout)

    assert output["integrity"] == "ok"
    assert output["journal_mode"] == "delete"
    assert output["schema_revision"] == "0001"
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert output["sha256"] == digest
    assert backup.with_suffix(".db.sha256").read_text().split()[0] == digest
    manifest = json.loads(backup.with_suffix(".db.json").read_text())
    assert manifest["kind"] == "music-agent-sqlite-backup"
    assert manifest["label"] == "test"
    assert manifest["bytes"] == backup.stat().st_size
    assert backup.stat().st_mode & 0o777 == 0o600

    connection = sqlite3.connect(backup)
    try:
        assert connection.execute("SELECT value FROM records").fetchone() == ("original",)
    finally:
        connection.close()


def test_verify_rejects_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "snapshot.db"
    create_database(source)
    run_helper("backup", "--source", str(source), "--destination", str(backup))
    with backup.open("ab") as stream:
        stream.write(b"tampered")

    result = run_helper("verify", "--require-checksum", str(backup), check=False)

    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr


def test_verify_requires_and_checks_backup_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "snapshot.db"
    create_database(source)
    run_helper("backup", "--source", str(source), "--destination", str(backup))

    manifest_path = backup.with_suffix(".db.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_revision"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_helper("verify", "--require-checksum", str(backup), check=False)
    assert result.returncode == 1
    assert "backup manifest mismatch" in result.stderr

    manifest_path.unlink()
    result = run_helper("verify", "--require-checksum", str(backup), check=False)
    assert result.returncode == 1
    assert "backup manifest is required" in result.stderr


def test_restore_atomically_replaces_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    destination = tmp_path / "destination.db"
    create_database(source, "snapshot")
    create_database(destination, "replace-me")
    run_helper("backup", "--source", str(source), "--destination", str(snapshot))

    run_helper(
        "restore",
        "--require-checksum",
        "--source",
        str(snapshot),
        "--destination",
        str(destination),
    )

    connection = sqlite3.connect(destination)
    try:
        assert connection.execute("SELECT value FROM records").fetchone() == ("snapshot",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        connection.close()


def test_restore_refuses_existing_sqlite_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot = tmp_path / "snapshot.db"
    destination = tmp_path / "destination.db"
    create_database(source, "snapshot")
    create_database(destination, "preserve-me")
    run_helper("backup", "--source", str(source), "--destination", str(snapshot))
    before = destination.read_bytes()
    Path(f"{destination}-journal").write_bytes(b"unexpected")

    result = run_helper(
        "restore",
        "--require-checksum",
        "--source",
        str(snapshot),
        "--destination",
        str(destination),
        check=False,
    )

    assert result.returncode == 1
    assert "SQLite sidecars exist" in result.stderr
    assert destination.read_bytes() == before


def test_backup_refuses_wal_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "snapshot.db"
    create_database(source)
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    finally:
        connection.close()

    result = run_helper(
        "backup", "--source", str(source), "--destination", str(backup), check=False
    )

    assert result.returncode == 1
    assert "unsafe SQLite journal mode" in result.stderr
    assert not backup.exists()


@pytest.mark.parametrize("sidecar", [".db.sha256", ".db.json"])
def test_backup_refuses_to_overwrite_existing_destination(tmp_path: Path, sidecar: str) -> None:
    source = tmp_path / "source.db"
    backup = tmp_path / "snapshot.db"
    create_database(source)
    backup.write_bytes(b"existing")

    result = run_helper(
        "backup", "--source", str(source), "--destination", str(backup), check=False
    )

    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    assert backup.read_bytes() == b"existing"
    assert not backup.with_suffix(sidecar).exists()
