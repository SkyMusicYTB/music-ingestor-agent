from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.models import (
    Base,
    Conversation,
    OpenAICall,
    OpenAIToolCall,
    Request,
    RequestTrack,
    Track,
    User,
)
from app.services.confirmation import confirmation_decision
from app.services.orchestration import OrchestrationService
from app.tools.registry import ToolDefinition, ToolRegistry


def database() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def request_row(
    factory: sessionmaker[Session], text: str = "Find one track", *, action: str = "find"
) -> str:
    with factory.begin() as session:
        user = User(
            username="listener",
            username_normalized="listener",
            password_hash="test-hash",  # noqa: S106 - inert fixture value
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Music")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text=text,
            action=action,
            input_kind="natural_language",
            requested_count=1 if action == "add" else None,
            status="pending",
            idempotency_key="test-request-1",
        )
        session.add(request)
        session.flush()
        return request.id


def proposal(
    *,
    tracks: list[dict[str, object]] | None = None,
    exhausted: bool | None = None,
) -> str:
    return json.dumps(
        {
            "summary": "A concise result",
            "clarification": None,
            "exhausted": not bool(tracks) if exhausted is None else exhausted,
            "tracks": tracks or [],
        }
    )


def track() -> dict[str, object]:
    return {
        "artist": "Massive Attack",
        "title": "Teardrop",
        "album": "Mezzanine",
        "album_artist": "Massive Attack",
        "year": 1998,
        "duration_seconds": 330.0,
        "recording_mbid": None,
        "release_mbid": None,
        "release_group_mbid": None,
        "source_url": None,
        "version": None,
        "rationale": "Matches the request.",
        "evidence": ["MusicBrainz search result"],
        "confidence": 0.9,
    }


def response(output: list[dict[str, object]], output_text: str = "") -> dict[str, object]:
    return {
        "id": "resp_test",
        "service_tier": "default",
        "output": output,
        "output_text": output_text,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }


class FakeOpenAI:
    configured = True
    model = "test-model"

    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.close_calls = 0

    async def create_response(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self) -> None:
        self.close_calls += 1


class UnsupportedWebError(RuntimeError):
    status_code = 400


def _canonical_track() -> dict[str, object]:
    value = track()
    value.update(
        {
            "recording_mbid": "11111111-1111-1111-1111-111111111111",
            "release_mbid": "22222222-2222-2222-2222-222222222222",
            "release_group_mbid": "33333333-3333-3333-3333-333333333333",
            "confidence": 1.0,
        }
    )
    return value


def settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        environment="test",
        music_path=tmp_path / "music",
        database_path=tmp_path / "test.db",
        max_agent_steps=6,
        max_agent_seconds=20,
        **overrides,
    )


@pytest.mark.asyncio
async def test_serial_tool_loop_replays_output_and_accounts_usage(tmp_path: Path) -> None:
    factory = database()
    request_id = request_row(factory)
    registry = ToolRegistry(factory)
    active = 0
    max_active = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, object]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        active -= 1
        return {"items": [arguments["query"]]}

    registry.register(
        ToolDefinition(
            name="search_test",
            description="Search test metadata",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=handler,
        )
    )
    first = response(
        [
            {"type": "reasoning", "id": "reasoning_1", "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "search_test",
                "arguments": json.dumps({"query": "trip hop"}),
            },
            {
                "type": "function_call",
                "call_id": "call_2",
                "name": "search_test",
                "arguments": json.dumps({"query": "downtempo"}),
            },
        ]
    )
    fake = FakeOpenAI([first, response([], proposal(tracks=[track()]))])
    service = OrchestrationService(settings(tmp_path), factory, registry, openai_client=fake)

    await service.run_request(request_id)

    with factory() as session:
        stored_request = session.get(Request, request_id)
        stored_track = session.scalar(
            select(RequestTrack).where(RequestTrack.request_id == request_id)
        )
        calls = list(session.scalars(select(OpenAICall).order_by(OpenAICall.created_at)))
        tool_calls = list(session.scalars(select(OpenAIToolCall)))
    assert stored_request is not None and stored_request.status == "preview"
    assert stored_track is not None and stored_track.title == "Teardrop"
    assert len(calls) == 2 and all(call.status == "completed" for call in calls)
    assert len(tool_calls) == 2
    assert max_active == 1
    second_input = fake.calls[1]["input_items"]
    assert any(item.get("encrypted_content") == "opaque" for item in second_input)
    assert sum(item.get("type") == "function_call_output" for item in second_input) == 2
    assert len(str(fake.calls[0]["safety_identifier"])) == 64
    assert fake.calls[0]["prompt_cache_key"] == fake.calls[1]["prompt_cache_key"]
    assert fake.calls[0]["max_tool_calls"] == 6


@pytest.mark.asyncio
async def test_only_server_verified_musicbrainz_evidence_can_auto_queue(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory, "Add Teardrop", action="add")
    registry = ToolRegistry(factory)

    async def recordings(_arguments: dict[str, Any]) -> dict[str, object]:
        return {
            "fallback_used": False,
            "matches": [
                {
                    "artist": "Massive Attack",
                    "title": "Teardrop",
                    "album": "Mezzanine",
                    "duration_seconds": 330.0,
                    "version": "studio",
                    "recording_mbid": "11111111-1111-1111-1111-111111111111",
                    "release_mbid": "22222222-2222-2222-2222-222222222222",
                    "release_group_mbid": "33333333-3333-3333-3333-333333333333",
                    "source": "musicbrainz",
                    "score": 96.0,
                    "decision": "auto",
                    "association_scope": "canonical_musicbrainz",
                    "lead": 10.0,
                }
            ],
        }

    registry.register(
        ToolDefinition(
            name="musicbrainz_search_recordings",
            description="Canonical recording fixture",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=recordings,
        )
    )
    first = response(
        [
            {
                "type": "function_call",
                "call_id": "call_musicbrainz",
                "name": "musicbrainz_search_recordings",
                "arguments": "{}",
            }
        ]
    )
    fake = FakeOpenAI([first, response([], proposal(tracks=[_canonical_track()]))])
    runtime_settings = settings(tmp_path)
    service = OrchestrationService(runtime_settings, factory, registry, openai_client=fake)

    await service.run_request(request_id)

    with factory() as session:
        stored_request = session.get(Request, request_id)
        stored_track = session.scalar(
            select(RequestTrack).where(RequestTrack.request_id == request_id)
        )
        assert stored_request is not None and stored_track is not None
        provenance = json.loads(stored_track.metadata_provenance_json)
        decision = confirmation_decision(stored_request, [stored_track], runtime_settings)
    assert provenance["automatic_association"] is True
    assert provenance["source"] == "musicbrainz_search_recordings"
    assert stored_track.metadata_confidence == pytest.approx(0.96)
    assert decision.auto_queue is True


@pytest.mark.asyncio
async def test_model_supplied_identifier_without_tool_binding_cannot_auto_queue(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory, "Add Teardrop", action="add")
    fake = FakeOpenAI([response([], proposal(tracks=[_canonical_track()]))])
    runtime_settings = settings(tmp_path)
    service = OrchestrationService(
        runtime_settings,
        factory,
        ToolRegistry(factory),
        openai_client=fake,
    )

    await service.run_request(request_id)

    with factory() as session:
        stored_request = session.get(Request, request_id)
        stored_track = session.scalar(
            select(RequestTrack).where(RequestTrack.request_id == request_id)
        )
        assert stored_request is not None and stored_track is not None
        provenance = json.loads(stored_track.metadata_provenance_json)
        decision = confirmation_decision(stored_request, [stored_track], runtime_settings)
    assert provenance["automatic_association"] is False
    assert stored_track.metadata_confidence is None
    assert decision.auto_queue is False


@pytest.mark.asyncio
async def test_missing_openai_key_marks_request_failed_without_startup_error(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory)
    service = OrchestrationService(settings(tmp_path), factory, ToolRegistry(factory))

    await service.run_request(request_id)

    with factory() as session:
        stored = session.get(Request, request_id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_code == "openai_not_configured"


@pytest.mark.asyncio
async def test_web_search_is_enabled_for_only_one_empty_result_fallback(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory)
    fake = FakeOpenAI(
        [
            response([], proposal()),
            response(
                [{"type": "web_search_call", "id": "web_1", "status": "completed"}],
                proposal(tracks=[track()]),
            ),
        ]
    )
    service = OrchestrationService(
        settings(tmp_path, openai_web_search_enabled=True),
        factory,
        ToolRegistry(factory),
        openai_client=fake,
    )

    await service.run_request(request_id)

    assert [call["enable_web_search"] for call in fake.calls] == [False, True]
    with factory() as session:
        stored = session.get(Request, request_id)
        calls = list(session.scalars(select(OpenAICall).order_by(OpenAICall.created_at)))
    assert stored is not None and stored.status == "preview"
    assert calls[1].web_search_count == 1
    assert calls[1].web_search_context == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_response", "status", "error_code"),
    [
        (
            {
                **response([], ""),
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            "incomplete",
            "openai_incomplete",
        ),
        (
            response(
                [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot help."}],
                    }
                ]
            ),
            "refused",
            "openai_refusal",
        ),
    ],
)
async def test_explicit_response_states_are_persisted(
    tmp_path: Path,
    terminal_response: dict[str, object],
    status: str,
    error_code: str,
) -> None:
    factory = database()
    request_id = request_row(factory)
    service = OrchestrationService(
        settings(tmp_path),
        factory,
        ToolRegistry(factory),
        openai_client=FakeOpenAI([terminal_response]),
    )

    await service.run_request(request_id)

    with factory() as session:
        stored = session.get(Request, request_id)
    assert stored is not None
    assert stored.status == status
    assert stored.error_code == error_code


@pytest.mark.asyncio
async def test_unsupported_web_search_gets_one_non_web_degraded_retry(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory)
    fake = FakeOpenAI(
        [
            response([], proposal()),
            UnsupportedWebError("web search is unsupported by this model"),
            response([], proposal()),
        ]
    )
    service = OrchestrationService(
        settings(tmp_path, openai_web_search_enabled=True),
        factory,
        ToolRegistry(factory),
        openai_client=fake,
    )

    await service.run_request(request_id)

    assert [call["enable_web_search"] for call in fake.calls] == [False, True, False]
    assert fake.calls[2]["tools"] == []
    with factory() as session:
        stored = session.get(Request, request_id)
        calls = list(session.scalars(select(OpenAICall).order_by(OpenAICall.created_at)))
    assert stored is not None and stored.status == "degraded"
    assert stored.error_code == "web_search_unsupported"
    assert [call.status for call in calls] == ["completed", "failed", "completed"]


@pytest.mark.asyncio
async def test_requested_count_replenishes_at_most_three_rounds(tmp_path: Path) -> None:
    factory = database()
    request_id = request_row(factory)
    with factory.begin() as session:
        stored = session.get(Request, request_id)
        assert stored is not None
        stored.requested_count = 4
    fake = FakeOpenAI([response([], proposal(exhausted=False)) for _ in range(4)])
    service = OrchestrationService(
        settings(tmp_path), factory, ToolRegistry(factory), openai_client=fake
    )

    await service.run_request(request_id)

    assert len(fake.calls) == 4
    combined_inputs = json.dumps(fake.calls[-1]["input_items"])
    assert "Replenishment round 3/3" in combined_inputs
    with factory() as session:
        stored = session.get(Request, request_id)
    assert stored is not None and stored.status == "incomplete"


@pytest.mark.asyncio
async def test_requested_count_replenishes_after_owned_candidates(tmp_path: Path) -> None:
    factory = database()
    request_id = request_row(factory)
    music_root = tmp_path / "music"
    music_root.mkdir()
    owned_path = music_root / "owned.mp3"
    owned_path.write_bytes(b"indexed fixture")
    with factory.begin() as session:
        stored = session.get(Request, request_id)
        assert stored is not None
        stored.requested_count = 3
        session.add(
            Track(
                artist="Massive Attack",
                artist_normalized="massive attack",
                title="Teardrop",
                title_normalized="teardrop",
                album="Mezzanine",
                version_signature="studio",
                filepath="owned.mp3",
                file_mtime_ns=owned_path.stat().st_mtime_ns,
                file_size=owned_path.stat().st_size,
            )
        )
    owned = track()
    first_tracks = [
        owned,
        {**track(), "artist": "Portishead", "title": "Roads"},
        {**track(), "artist": "Tricky", "title": "Hell Is Round the Corner"},
        {**owned},
    ]
    # Give the repeated owned row a different identity that still resolves to
    # the same exact indexed title/version through its MBID-free text identity.
    first_tracks[-1] = {**owned, "rationale": "Repeated provider evidence."}
    additional = {**track(), "artist": "UNKLE", "title": "Rabbit in Your Headlights"}
    fake = FakeOpenAI(
        [
            response([], proposal(tracks=first_tracks, exhausted=False)),
            response([], proposal(tracks=[additional], exhausted=True)),
        ]
    )
    service = OrchestrationService(
        settings(tmp_path), factory, ToolRegistry(factory), openai_client=fake
    )

    await service.run_request(request_id)

    assert len(fake.calls) == 2
    assert "library-owned tracks" in json.dumps(fake.calls[1]["input_items"])
    with factory() as session:
        stored = session.get(Request, request_id)
        candidates = list(
            session.scalars(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
            )
        )
    assert stored is not None and stored.selected_count == 3
    assert [candidate.duplicate_status for candidate in candidates].count("owned") == 1


@pytest.mark.asyncio
async def test_expired_orchestration_lease_is_resumed(tmp_path: Path) -> None:
    factory = database()
    request_id = request_row(factory)
    with factory.begin() as session:
        stored = session.get(Request, request_id)
        assert stored is not None
        stored.status = "orchestrating"
        stored.lease_token = "expired-owner-token"  # noqa: S105 - inert fixture value
        stored.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    fake = FakeOpenAI([response([], proposal(tracks=[track()]))])
    service = OrchestrationService(
        settings(tmp_path), factory, ToolRegistry(factory), openai_client=fake
    )

    await service.run_request(request_id)

    with factory() as session:
        stored = session.get(Request, request_id)
    assert stored is not None and stored.status == "preview"
    assert stored.lease_token is None and stored.lease_expires_at is None


@pytest.mark.asyncio
async def test_source_selector_accepts_only_supplied_ids_and_omits_urls(
    tmp_path: Path,
) -> None:
    factory = database()
    request_id = request_row(factory)
    fake = FakeOpenAI(
        [
            response(
                [],
                json.dumps(
                    {
                        "selected_source_id": "source_A",
                        "needs_review": False,
                        "rationale": "Exact artist, title, and duration.",
                    }
                ),
            )
        ]
    )
    service = OrchestrationService(
        settings(tmp_path), factory, ToolRegistry(factory), openai_client=fake
    )

    result = await service.select_source(
        {
            "request_id": request_id,
            "job_id": "job_1",
            "intent": {
                "artist": "Artist",
                "title": "Song",
                "album": None,
                "version": "studio",
                "duration_seconds": 200,
            },
            "candidates": [
                {
                    "source_id": "source_A",
                    "title": "Artist - Song",
                    "channel": "Artist",
                    "duration_seconds": 201,
                    "url": "https://example.invalid/must-not-leak",
                }
            ],
        }
    )

    assert result["selected_source_id"] == "source_A"
    encoded_input = json.dumps(fake.calls[0]["input_items"])
    assert "example.invalid" not in encoded_input
    enum = fake.calls[0]["text_format"]["schema"]["properties"]["selected_source_id"]["enum"]
    assert enum == ["source_A", None]


@pytest.mark.asyncio
async def test_orchestration_close_is_idempotent_and_closes_both_dependencies(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    registry_close_calls = 0

    async def close_registry_resource() -> None:
        nonlocal registry_close_calls
        registry_close_calls += 1

    registry.add_closer(close_registry_resource)
    fake = FakeOpenAI([])
    service = OrchestrationService(settings(tmp_path), database(), registry, openai_client=fake)

    await service.aclose()
    await service.aclose()

    assert registry_close_calls == 1
    assert fake.close_calls == 1
