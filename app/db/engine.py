from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings

EXPECTED_SCHEMA_REVISION = "0002"


def create_database_engine(settings: Settings) -> Engine:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        settings.sqlite_url,
        connect_args={"timeout": 10.0, "check_same_thread": False},
        poolclass=NullPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        assert isinstance(dbapi_connection, sqlite3.Connection)
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def assert_database_pragmas(engine: Engine) -> None:
    with engine.connect() as connection:
        mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        synchronous = connection.execute(text("PRAGMA synchronous")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
    if str(mode).lower() != "delete" or int(synchronous) != 2 or int(foreign_keys) != 1:
        raise RuntimeError("unsafe SQLite pragma configuration")


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        ).scalar_one_or_none()
        if not exists:
            return None
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()


def assert_schema_current(engine: Engine) -> None:
    revision = current_revision(engine)
    if revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError(
            f"database schema is {revision or 'uninitialized'}, "
            f"expected {EXPECTED_SCHEMA_REVISION}; "
            "run `music-agent migrate`"
        )


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        result = destination_conn.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"backup integrity check failed: {result}")
    finally:
        destination_conn.close()
        source_conn.close()
