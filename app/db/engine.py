from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import URL, Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings

EXPECTED_SCHEMA_REVISION = "0004"


def create_database_engine(settings: Settings, *, read_only: bool = False) -> Engine:
    if not read_only:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    # The native SQLite URI is independently escaped from SQLAlchemy's URL.
    # mode=ro refuses missing files even when the containing directory is writable.
    database_path = settings.database_path.absolute()
    url: str | URL = (
        URL.create(
            "sqlite+pysqlite",
            database=database_path.as_uri(),
            query={"mode": "ro", "uri": "true"},
        )
        if read_only
        else settings.sqlite_url
    )
    engine = create_engine(
        url,
        connect_args={"timeout": 10.0, "check_same_thread": False},
        poolclass=NullPool,
        future=True,
    )

    if read_only:

        @event.listens_for(engine, "do_connect")
        def guard_read_only_database(
            _dialect: object, _connection_record: object, _args: object, _kwargs: object
        ) -> None:
            # SQLite can create WAL sidecars even for mode=ro. Production uses
            # rollback journals, so fail closed before opening a WAL database;
            # immutable=1 would incorrectly ignore changes to a live database.
            with database_path.open("rb") as database:
                header = database.read(20)
            if header[:16] == b"SQLite format 3\x00" and header[18:20] != b"\x01\x01":
                raise RuntimeError("read-only inspection requires rollback-journal SQLite")

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        assert isinstance(dbapi_connection, sqlite3.Connection)
        cursor = dbapi_connection.cursor()
        if read_only:
            cursor.execute("PRAGMA query_only=ON")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        if not read_only:
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def immediate_session(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    """Serialize read/check/write invariants without holding a lock over external work."""
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


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
