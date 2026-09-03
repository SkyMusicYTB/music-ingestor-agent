from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Session as DbSession
from app.db.models import User
from app.services.security import SESSION_COOKIE


@pytest.fixture
def idle_browser_session(client, session_factory):
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
    return user_id, old_activity, idle_deadline


@pytest.mark.parametrize(
    "path",
    [
        "/downloads?fragment=true",
        "/downloads?fragment=1",
        "/library?fragment=true",
        "/api/v1/library/scan-status",
    ],
)
def test_repeated_automatic_refresh_does_not_extend_idle_session(
    client, session_factory, idle_browser_session, path
) -> None:
    user_id, old_activity, idle_deadline = idle_browser_session
    for _ in range(3):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 200
        with session_factory() as session:
            row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
            assert row.last_activity_at.replace(tzinfo=UTC) == old_activity
            assert row.idle_expires_at.replace(tzinfo=UTC) == idle_deadline


@pytest.mark.parametrize(
    "path", ["/downloads", "/downloads?fragment=false", "/library", "/library?fragment=false"]
)
def test_interactive_full_pages_still_extend_idle_session(
    client, session_factory, idle_browser_session, path
) -> None:
    user_id, old_activity, idle_deadline = idle_browser_session
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200
    with session_factory() as session:
        row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
        assert row.last_activity_at.replace(tzinfo=UTC) > old_activity
        assert row.idle_expires_at.replace(tzinfo=UTC) > idle_deadline


@pytest.mark.parametrize(
    "path",
    ["/downloads?fragment=true", "/library?fragment=true", "/api/v1/library/scan-status"],
)
@pytest.mark.parametrize(
    ("state", "status_code"), [("expired", 401), ("disabled", 401), ("forced_change", 403)]
)
def test_background_requests_retain_authentication_and_forced_change_checks(
    client, session_factory, idle_browser_session, path, state, status_code
) -> None:
    user_id, _old_activity, _idle_deadline = idle_browser_session
    with session_factory.begin() as session:
        if state == "expired":
            row = session.scalar(select(DbSession).where(DbSession.user_id == user_id))
            row.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            user = session.get(User, user_id)
            if state == "disabled":
                user.is_active = False
            else:
                user.must_change_password = True
    response = client.get(path, follow_redirects=False)
    assert response.status_code == status_code
    if state == "forced_change":
        assert response.json()["detail"]["code"] == "password_change_required"
