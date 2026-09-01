from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig

from app.config import Settings
from app.db.engine import (
    assert_database_pragmas,
    assert_schema_current,
    create_database_engine,
    make_session_factory,
)
from app.main import create_app
from app.repositories.auth import AuthRepository
from app.services.library_scan import LibraryScanner


def _alembic(settings: Settings) -> AlembicConfig:
    config = AlembicConfig(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.sqlite_url)
    return config


def migrate(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic(settings), "head")
    engine = create_database_engine(settings)
    try:
        assert_schema_current(engine)
        assert_database_pragmas(engine)
    finally:
        engine.dispose()


def validate(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        assert_schema_current(engine)
        assert_database_pragmas(engine)
        for path in (
            settings.database_path.parent,
            settings.artwork_path,
            settings.downloads_path,
            settings.music_path,
            settings.backup_path,
        ):
            if not path.exists():
                raise RuntimeError(f"required path does not exist: {path}")
            if not os.access(path, os.R_OK):
                raise RuntimeError(f"required path is not readable: {path}")
    finally:
        engine.dispose()
    print("configuration, schema, SQLite pragmas, and paths are valid")


def admin_reset(settings: Settings) -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("admin-reset must run from an interactive terminal")
    username = input("Admin username: ").strip()
    password = getpass.getpass("New password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("passwords do not match")
    engine = create_database_engine(settings)
    factory = make_session_factory(engine)
    try:
        assert_schema_current(engine)
        AuthRepository(engine, factory, settings).reset_admin(username, password)
    finally:
        engine.dispose()
    print("admin credentials reset and all sessions revoked")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the database schema")
    commands.add_parser("validate", help="validate config, paths, schema and SQLite")
    scan = commands.add_parser("scan", help="index the music library")
    scan.add_argument("--full", action="store_true")
    commands.add_parser("admin-reset", help="reset the initial admin from a TTY")
    commands.add_parser("web", help="run the web service")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "migrate":
        migrate(settings)
    elif args.command == "validate":
        validate(settings)
    elif args.command == "admin-reset":
        admin_reset(settings)
    elif args.command == "scan":
        engine = create_database_engine(settings)
        factory = make_session_factory(engine)
        try:
            assert_schema_current(engine)
            result = LibraryScanner(factory, settings.music_path).run(full=args.full)
        finally:
            engine.dispose()
        print(
            f"scan {result.status}: {result.scanned_files} files, "
            f"{result.changed_files} changed, {result.error_count} errors"
        )
    elif args.command == "web":
        uvicorn.run(
            create_app(settings),
            host=settings.bind_host,
            port=settings.bind_port,
            proxy_headers=False,
            server_header=False,
        )


if __name__ == "__main__":
    main()
