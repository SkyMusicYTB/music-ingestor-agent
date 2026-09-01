from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.events import _event_stream, _initial_event_id, _last_event_id


def test_fresh_sse_connection_starts_at_current_tail() -> None:
    assert _last_event_id(None) is None
    assert _last_event_id("invalid") is None


def test_reconnecting_sse_connection_retains_explicit_cursor() -> None:
    assert _last_event_id("42") == 42
    assert _last_event_id("-5") == 0


def test_page_cursor_is_used_until_last_event_id_exists() -> None:
    assert _initial_event_id(None, 17) == 17
    assert _initial_event_id("42", 17) == 42
    assert _initial_event_id("invalid", 17) == 17


@pytest.mark.asyncio
async def test_event_after_page_cursor_is_replayed_during_stream_setup() -> None:
    class Events:
        def __init__(self) -> None:
            self.after_calls: list[int] = []

        def bounds(self) -> tuple[int, int]:
            # Event 8 committed after the page captured cursor 7 but before the
            # EventSource connection began iterating.
            return 5, 8

        def after(self, event_id: int) -> list[SimpleNamespace]:
            self.after_calls.append(event_id)
            return [
                SimpleNamespace(
                    id=8,
                    entity_type="request",
                    entity_id="request-1",
                    event_type="request.preview",
                    message="Proposal ready",
                    details_json="{}",
                    created_at=datetime.now(UTC),
                )
            ]

    class Request:
        def __init__(self, events: Events) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(events=events))

        async def is_disconnected(self) -> bool:
            return False

    repository = Events()
    stream = _event_stream(Request(repository), _initial_event_id(None, 7))  # type: ignore[arg-type]
    try:
        message = await anext(stream)
    finally:
        await stream.aclose()

    assert repository.after_calls == [7]
    assert message.startswith("id: 8\nevent: update\n")
