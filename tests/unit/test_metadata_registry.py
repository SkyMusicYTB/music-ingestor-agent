from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import Field
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.models import Base
from app.schemas import StrictModel
from app.tools.media_sources import build_media_source_tools
from app.tools.registry import ToolDefinition, ToolRegistry, build_default_registry


class Arguments(StrictModel):
    query: str = Field(min_length=1, max_length=20)


def database() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_default_registry_has_only_locked_metadata_tools() -> None:
    registry = build_default_registry(
        Settings(
            environment="test",
            musicbrainz_user_agent="MusicAgentTests/1.0 (tests@example.test)",
        ),
        database(),
    )
    assert {definition.name for definition in registry.definitions} == {
        "search_library",
        "get_library_summary",
        "musicbrainz_search_recordings",
        "musicbrainz_search_releases",
        "listenbrainz_popular_recordings",
        "listenbrainz_artist_radio",
        "listenbrainz_user_recommendations",
    }
    schemas = {tool["name"]: tool["parameters"] for tool in registry.openai_tools()}
    assert schemas["search_library"]["properties"]["limit"]["maximum"] == 50
    assert schemas["musicbrainz_search_recordings"]["properties"]["limit"]["maximum"] == 25
    assert schemas["listenbrainz_popular_recordings"]["properties"]["limit"]["maximum"] == 100
    await registry.aclose()


@pytest.mark.asyncio
async def test_production_registry_adds_only_finite_media_broker_tools() -> None:
    factory = database()
    registry = build_default_registry(
        Settings(
            environment="test",
            musicbrainz_user_agent="MusicAgentTests/1.0 (tests@example.test)",
        ),
        factory,
        media_source_tools=build_media_source_tools(factory),
    )
    names = {definition.name for definition in registry.definitions}
    assert {"search_media_sources", "probe_media_source"} <= names
    assert "youtube_search_candidates" not in names
    await registry.aclose()


@pytest.mark.asyncio
async def test_registry_is_read_only_strict_and_cached() -> None:
    count = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, object]:
        nonlocal count
        values = Arguments.model_validate(arguments)
        count += 1
        return {"items": [values.query]}

    registry = ToolRegistry(database())
    registry.register(
        ToolDefinition(
            name="search_test",
            description="test",
            parameters=Arguments.model_json_schema(),
            handler=handler,
            cache_ttl_seconds=60,
        )
    )
    schema = registry.openai_tools()[0]
    assert schema["strict"] is True
    assert schema["parameters"]["additionalProperties"] is False

    first = await registry.execute("search_test", json.dumps({"query": "value"}))
    second = await registry.execute("search_test", json.dumps({"query": "value"}))
    invalid = await registry.execute("search_test", json.dumps({"query": "x", "extra": 1}))

    assert first.status == "completed"
    assert second.summary["cached"] is True
    assert invalid.status == "failed"
    assert count == 1

    with pytest.raises(ValueError, match="read-only"):
        registry.register(
            ToolDefinition(
                name="write_test",
                description="unsafe",
                parameters={"type": "object", "properties": {}},
                handler=handler,
                read_only=False,
            )
        )


@pytest.mark.asyncio
async def test_registry_closes_every_resource_once_even_when_one_fails() -> None:
    registry = ToolRegistry()
    closed: list[str] = []

    async def succeeds() -> None:
        closed.append("success")

    async def fails() -> None:
        closed.append("failure")
        raise RuntimeError("close failed")

    registry.add_closer(succeeds)
    registry.add_closer(fails)

    with pytest.raises(RuntimeError, match="close failed"):
        await registry.aclose()
    await registry.aclose()

    assert closed == ["failure", "success"]
