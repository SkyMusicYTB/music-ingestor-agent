from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import cli
from app.db.engine import assert_schema_current, create_database_engine
from app.db.models import User


def _snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_mode,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("command", ["library-audit", "user-list"])
def test_readonly_cli_never_creates_missing_database_parent(
    settings, tmp_path, monkeypatch, command
):
    missing = tmp_path / "missing-parent" / "nested" / "music.db"
    configured = settings.model_copy(update={"database_path": missing})
    monkeypatch.setattr(cli, "Settings", lambda: configured)
    with pytest.raises((FileNotFoundError, OperationalError)):
        cli.main([command, "--json"])
    assert not (tmp_path / "missing-parent").exists()


def test_readonly_engine_refuses_writes_even_when_query_guard_is_disabled(settings, engine):
    before = settings.database_path.read_bytes()
    readonly = create_database_engine(settings, read_only=True)
    try:
        assert_schema_current(readonly)
        with readonly.connect() as connection:
            assert connection.scalar(text("PRAGMA query_only")) == 1
            assert connection.scalar(text("PRAGMA journal_mode")) == "delete"
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("CREATE TABLE forbidden_write (value INTEGER)"))
            connection.rollback()
            connection.execute(text("PRAGMA query_only=OFF"))
            with pytest.raises(OperationalError, match="readonly"):
                connection.execute(text("CREATE TABLE forbidden_write (value INTEGER)"))
        assert settings.database_path.read_bytes() == before
    finally:
        readonly.dispose()


@pytest.mark.parametrize("command", ["library-audit", "user-list"])
def test_readonly_cli_and_schema_check_preserve_database_bytes_and_files(
    settings, engine, session_factory, tmp_path, monkeypatch, capsys, command
):
    with session_factory.begin() as session:
        session.add(
            User(
                username="owner",
                username_normalized="owner",
                password_hash="private-password-hash-must-not-appear",  # noqa: S106 - sentinel
                role="admin",
            )
        )
    marker = settings.music_path / "keep-this-unrelated-file.txt"
    marker.write_text("An existing file must remain untouched.", encoding="utf-8")
    before = _snapshot(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    for _ in range(2):
        cli.main([command, "--json"])
        output = capsys.readouterr().out
        assert "private-password-hash" not in output
        assert isinstance(json.loads(output), dict)
    readonly = create_database_engine(settings, read_only=True)
    try:
        assert_schema_current(readonly)
    finally:
        readonly.dispose()
    assert _snapshot(tmp_path) == before


def test_readonly_engine_escapes_literal_uri_characters(settings, tmp_path):
    database_path = tmp_path / "literal # percent % unicode é.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE marker (value INTEGER)")
        connection.execute("INSERT INTO marker VALUES (42)")
    configured = settings.model_copy(update={"database_path": database_path})
    readonly = create_database_engine(configured, read_only=True)
    try:
        with readonly.connect() as connection:
            assert connection.scalar(text("SELECT value FROM marker")) == 42
    finally:
        readonly.dispose()


def test_readonly_engine_rejects_wal_before_it_can_create_sidecars(settings, tmp_path):
    database_path = tmp_path / "wal.db"
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE marker (value INTEGER)")
        connection.commit()
    finally:
        connection.close()
    assert not database_path.with_name("wal.db-wal").exists()
    assert not database_path.with_name("wal.db-shm").exists()
    before = _snapshot(tmp_path)
    readonly = create_database_engine(
        settings.model_copy(update={"database_path": database_path}), read_only=True
    )
    try:
        with pytest.raises(RuntimeError, match="rollback-journal"), readonly.connect():
            pytest.fail("WAL inspection must be rejected before SQLite opens it")
    finally:
        readonly.dispose()
    assert _snapshot(tmp_path) == before
