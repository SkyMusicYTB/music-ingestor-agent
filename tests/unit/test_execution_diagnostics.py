from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.api.usage import latest_execution_summary
from app.db.models import OpenAICall

PASSWORD = "diagnostics-fixture-password"  # noqa: S105 - inert test credential


def _execution(*, at: datetime, reason: str | None, round_number: int | None) -> OpenAICall:
    return OpenAICall(
        model="test-model",
        prompt_version="private-prompt-version-sentinel",
        prompt_hash="0" * 64,
        status="failed" if reason else "started",
        model_round=round_number,
        configured_model_rounds=50,
        configured_tool_calls=10,
        configured_agent_seconds=120,
        termination_reason=reason,
        error_code="private-provider-error-sentinel",
        provider_error_parameter="private-provider-parameter-sentinel",
        created_at=at,
    )


def test_latest_execution_summary_is_bounded_and_ignores_running_or_auxiliary_calls(
    session_factory,
):
    assert latest_execution_summary(session_factory) is None
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        session.add_all(
            [
                _execution(at=now, reason="normal_synthesis", round_number=2),
                _execution(
                    at=now + timedelta(seconds=1), reason="wall_time_exhaustion", round_number=8
                ),
                _execution(
                    at=now + timedelta(seconds=2), reason="provider_failure", round_number=None
                ),
                _execution(at=now + timedelta(seconds=3), reason=None, round_number=1),
            ]
        )
    result = latest_execution_summary(session_factory)
    assert result == {
        "recorded_at": (now + timedelta(seconds=1)).replace(tzinfo=None).isoformat(),
        "model_rounds_used": 8,
        "configured_model_rounds": 50,
        "configured_tool_calls": 10,
        "configured_agent_seconds": 120,
        "termination_reason": "wall_time_exhaustion",
    }
    with session_factory.begin() as session:
        session.add(
            _execution(
                at=now + timedelta(seconds=4), reason="private-unknown-reason", round_number=999
            )
        )
    result = latest_execution_summary(session_factory)
    assert result is not None
    assert result["termination_reason"] == "unknown"
    assert result["model_rounds_used"] is None
    assert "private" not in repr(result)


def test_execution_summary_is_admin_only_and_public_readiness_stays_minimal(
    client, session_factory, monkeypatch
):
    token = client.get("/setup").cookies["music_agent_preauth"]
    assert (
        client.post(
            "/setup",
            data={
                "username": "operator",
                "password": PASSWORD,
                "csrf_token": token,
                "acknowledge_rights": "yes",
            },
            follow_redirects=False,
        ).status_code
        == 303
    )
    with session_factory.begin() as session:
        session.add(_execution(at=datetime.now(UTC), reason="wall_time_exhaustion", round_number=8))
    for path in ("/settings", "/health", "/api/v1/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert "wall_time_exhaustion" in response.text
        assert "private-prompt" not in response.text
        assert "private-provider" not in response.text
    summary = client.get("/api/v1/health").json()["checks"]["last_model_execution"]
    assert summary["model_rounds_used"] == 8
    assert summary["configured_model_rounds"] == 50
    assert summary["configured_agent_seconds"] == 120

    created = client.post(
        "/api/v1/admin/users",
        json={
            "username": "listener",
            "temporary_password": PASSWORD,
            "must_change_password": False,
        },
        headers={"X-CSRF-Token": client.cookies["music_agent_csrf"]},
    )
    assert created.status_code == 201
    client.cookies.clear()
    token = client.get("/login").cookies["music_agent_preauth"]
    assert (
        client.post(
            "/login",
            data={"username": "listener", "password": PASSWORD, "csrf_token": token},
            follow_redirects=False,
        ).status_code
        == 303
    )
    for path in ("/settings", "/health", "/api/v1/health"):
        response = client.get(path)
        assert response.status_code == 403
        assert "wall_time_exhaustion" not in response.text

    def forbidden_summary(_factory):
        raise AssertionError("public readiness must not query execution diagnostics")

    monkeypatch.setattr("app.api.health.latest_execution_summary", forbidden_summary)
    client.cookies.clear()
    assert client.get("/health/ready").json() == {"status": "ready"}
