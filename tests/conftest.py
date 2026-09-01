from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.engine import create_database_engine, make_session_factory
from app.db.models import Base
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    paths = {
        "database_path": tmp_path / "music-agent.db",
        "artwork_path": tmp_path / "artwork",
        "downloads_path": tmp_path / "downloads",
        "music_path": tmp_path / "music",
        "backup_path": tmp_path / "backups",
    }
    for path in paths.values():
        (path if path.suffix == "" else path.parent).mkdir(parents=True, exist_ok=True)
    return Settings(
        environment="test",
        trusted_hosts=["testserver"],
        auth_hmac_key="test-auth-hmac-key-with-enough-entropy",
        initial_scan_required=False,
        openai_api_key=None,
        **paths,
    )


@pytest.fixture
def engine(settings: Settings) -> Engine:
    result = create_database_engine(settings)
    Base.metadata.create_all(result)
    with result.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0001')"))
    yield result
    result.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


@pytest.fixture
def client(settings: Settings, engine: Engine) -> TestClient:
    # create_app creates its own NullPool engine pointing at the same migrated file.
    with TestClient(create_app(settings), client=("127.0.0.1", 5050)) as test_client:
        yield test_client
