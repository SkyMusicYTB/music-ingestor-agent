from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import events as events_module
from app.api.events import _event_stream, _initial_event_id, _last_event_id
from app.db.models import Session as DbSession
from app.db.models import User
from app.services.security import SESSION_COOKIE


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


@pytest.mark.parametrize("value", [str(2**63), str(2**64 - 1), "9" * 5000])
def test_oversized_header_cursor_never_reaches_sqlite(value: str) -> None:
    assert _last_event_id(value) is None
    assert _initial_event_id(value, 17) == 17
    assert _initial_event_id(value, None) is None


def test_header_cursor_accepts_signed_sqlite_upper_bound() -> None:
    assert _last_event_id(str(2**63 - 1)) == 2**63 - 1


def test_sse_initial_connections_and_reconnects_never_extend_idle_session(
    client, session_factory, monkeypatch
) -> None:
    auth = client.app.state.auth
    user_id = auth.create_initial_admin("owner", "correct horse battery staple")
    token = auth.create_session(user_id)
    client.cookies.set(SESSION_COOKIE, token.token)
    old_activity = datetime.now(UTC) - timedelta(minutes=2)
    idle_deadline = datetime.now(UTC) + timedelta(minutes=3)
    with session_factory.begin() as session:
        row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
        row.last_activity_at = old_activity
        row.idle_expires_at = idle_deadline

    cursors = []

    async def finite_stream(_request, last_id):
        cursors.append(last_id)
        yield ": heartbeat\n\n"

    monkeypatch.setattr(events_module, "_event_stream", finite_stream)
    for header in [None, "19", str(2**63)]:
        headers = {"Last-Event-ID": header} if header is not None else {}
        response = client.get("/api/v1/events?after=17", headers=headers)
        assert response.status_code == 200
        assert response.text == ": heartbeat\n\n"
        with session_factory() as session:
            row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
            assert row.last_activity_at.replace(tzinfo=UTC) == old_activity
            assert row.idle_expires_at.replace(tzinfo=UTC) == idle_deadline
    assert cursors == [17, 19, 17]


@pytest.mark.parametrize(
    ("state", "status_code"),
    [
        ("missing", 401),
        ("idle_expired", 401),
        ("absolute_expired", 401),
        ("revoked", 401),
        ("disabled", 401),
        ("forced_change", 403),
    ],
)
def test_sse_initial_no_touch_auth_retains_all_access_checks(
    client, session_factory, monkeypatch, state, status_code
) -> None:
    auth = client.app.state.auth
    user_id = auth.create_initial_admin("owner", "correct horse battery staple")
    token = auth.create_session(user_id)
    if state != "missing":
        client.cookies.set(SESSION_COOKIE, token.token)
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
        user = session.get(User, user_id)
        if state == "idle_expired":
            row.idle_expires_at = now - timedelta(seconds=1)
        elif state == "absolute_expired":
            row.absolute_expires_at = now - timedelta(seconds=1)
        elif state == "revoked":
            row.revoked_at = now
        elif state == "disabled":
            user.is_active = False
        elif state == "forced_change":
            user.must_change_password = True

    async def forbidden_stream(_request, _last_id):
        pytest.fail("an unauthorized connection must not begin streaming")
        yield ""  # pragma: no cover

    monkeypatch.setattr(events_module, "_event_stream", forbidden_stream)
    response = client.get("/api/v1/events", follow_redirects=False)
    assert response.status_code == status_code
    if state == "forced_change":
        assert response.json()["detail"]["code"] == "password_change_required"


@pytest.mark.asyncio
async def test_event_after_page_cursor_is_replayed_during_stream_setup() -> None:
    class Events:
        def __init__(self) -> None:
            self.after_calls: list[int] = []

        def bounds(self) -> tuple[int, int]:
            # Event 8 committed after the page captured cursor 7 but before the
            # EventSource connection began iterating.
            return 5, 8

        def visible_after(self, event_id: int, **scope) -> list[SimpleNamespace]:
            assert scope["user_id"] == "user-1"
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
            self.cookies = {}
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    events=events,
                    auth=SimpleNamespace(
                        resolve_session=lambda token, touch: SimpleNamespace(
                            user_id="user-1", role="user", must_change_password=False
                        )
                    ),
                )
            )

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
