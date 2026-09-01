from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, Index, inspect

from app.cli import migrate, validate
from app.config import Settings
from app.db.engine import create_database_engine, current_revision
from app.db.models import Base
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIRECTORY = REPO_ROOT / "migrations" / "versions"


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).split())


def _index_signature(index: Index) -> tuple[str | None, tuple[str, ...], bool, str | None]:
    predicate = index.dialect_options["sqlite"].get("where")
    return (
        index.name,
        tuple(column.name for column in index.columns),
        bool(index.unique),
        _normalized_sql(predicate) if predicate is not None else None,
    )


def _reflected_index_signature(
    index: Mapping[str, object],
) -> tuple[str | None, tuple[str, ...], bool, str | None]:
    options = index.get("dialect_options")
    dialect_options = options if isinstance(options, Mapping) else {}
    predicate = dialect_options.get("sqlite_where")
    column_names = index.get("column_names")
    assert isinstance(column_names, list)
    return (
        str(index["name"]) if index.get("name") is not None else None,
        tuple(str(column) for column in column_names),
        bool(index.get("unique")),
        _normalized_sql(predicate) if predicate is not None else None,
    )


def _assert_schema_matches_metadata(database_path: Path) -> None:
    settings = Settings(environment="test", database_path=database_path)
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []

        inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            expected_checks = {
                (constraint.name, _normalized_sql(constraint.sqltext))
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            reflected_checks = {
                (
                    str(constraint["name"]) if constraint.get("name") is not None else None,
                    _normalized_sql(constraint["sqltext"]),
                )
                for constraint in inspector.get_check_constraints(table.name)
            }
            assert reflected_checks == expected_checks, table.name

            expected_indexes = {_index_signature(index) for index in table.indexes}
            reflected_indexes = {
                _reflected_index_signature(index) for index in inspector.get_indexes(table.name)
            }
            assert reflected_indexes == expected_indexes, table.name
    finally:
        engine.dispose()


def test_fresh_migration_persists_revision_and_is_idempotent(settings: Settings) -> None:
    migrate(settings)
    migrate(settings)

    engine = create_database_engine(settings)
    try:
        assert current_revision(engine) == "0001"
    finally:
        engine.dispose()
    validate(settings)
    _assert_schema_matches_metadata(settings.database_path)

    with TestClient(create_app(settings), client=("127.0.0.1", 5050)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/setup").status_code == 200


def test_installed_wheel_fresh_migration_matches_current_metadata(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheel"
    installed_directory = tmp_path / "installed"
    runtime_directory = tmp_path / "runtime"
    wheel_directory.mkdir()
    runtime_directory.mkdir()
    subprocess.run(  # noqa: S603 - fixed interpreter builds the repository test fixture
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_directory.glob("music_agent-*.whl"))
    assert len(wheels) == 1
    subprocess.run(  # noqa: S603 - fixed interpreter installs the repository test fixture
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--target",
            str(installed_directory),
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    database_path = runtime_directory / "music-agent.db"
    paths = {
        "MUSIC_AGENT_DATABASE_PATH": database_path,
        "MUSIC_AGENT_ARTWORK_PATH": runtime_directory / "artwork",
        "MUSIC_AGENT_DOWNLOADS_PATH": runtime_directory / "downloads",
        "MUSIC_AGENT_MUSIC_PATH": runtime_directory / "music",
        "MUSIC_AGENT_BACKUP_PATH": runtime_directory / "backups",
    }
    for path in paths.values():
        if path != database_path:
            path.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MUSIC_AGENT_")
    }
    environment.update(
        {
            "MUSIC_AGENT_ENVIRONMENT": "test",
            "PYTHONPATH": str(installed_directory),
            **{key: str(value) for key, value in paths.items()},
        }
    )
    result = subprocess.run(  # noqa: S603 - installed wheel is the test fixture
        [
            sys.executable,
            "-c",
            "import app; from app.cli import main; "
            "print(app.__file__); main(['migrate']); main(['migrate'])",
        ],
        cwd=runtime_directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(installed_directory) in result.stdout
    _assert_schema_matches_metadata(database_path)


def test_migration_history_does_not_import_live_models() -> None:
    migrations = sorted(MIGRATION_DIRECTORY.glob("*.py"))
    assert migrations
    for migration in migrations:
        source = migration.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(migration))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "app.db.models" not in imported_modules, migration.name
        assert "Base.metadata" not in source, migration.name
