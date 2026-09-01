from __future__ import annotations

import hashlib
import re
from typing import cast

from app.db.models import ArtworkCache


def _setup(client) -> None:
    page = client.get("/setup")
    token = page.cookies["music_agent_preauth"]
    response = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "correct horse battery staple",
            "csrf_token": token,
            "acknowledge_rights": "yes",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_setup_closes_and_authenticated_request_is_idempotent(client) -> None:
    _setup(client)
    assert client.get("/setup").status_code == 404
    csrf = client.cookies["music_agent_csrf"]
    headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": "test-request-0001",
    }
    body = {"text": "Numb by Linkin Park", "action": "find", "conversation_id": None}
    first = client.post("/api/v1/requests", json=body, headers=headers)
    second = client.post("/api/v1/requests", json=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["request"]["id"] == first.json()["request"]["id"]


def test_mutation_rejects_cross_origin_and_bad_csrf(client) -> None:
    _setup(client)
    body = {"text": "a song", "action": "find", "conversation_id": None}
    response = client.post(
        "/api/v1/requests",
        json=body,
        headers={
            "Origin": "http://evil.invalid",
            "X-CSRF-Token": "wrong",
            "Idempotency-Key": "test-request-0002",
        },
    )
    assert response.status_code == 403


def test_mutation_rejects_missing_origin_even_with_valid_csrf(client) -> None:
    _setup(client)
    response = client.post(
        "/api/v1/requests",
        json={"text": "a song", "action": "find", "conversation_id": None},
        headers={
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
            "Idempotency-Key": "test-request-0003",
        },
    )
    assert response.status_code == 403


def test_security_headers_are_present(client) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_authenticated_artwork_route_accepts_internal_namespaced_key(client) -> None:
    _setup(client)
    data = b"cached-image"
    digest = hashlib.sha256(data).hexdigest()
    relative = f"{digest[:2]}/{digest}.jpg"
    path = client.app.state.settings.artwork_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    key = "caa-release:11111111-1111-1111-1111-111111111111"
    with client.app.state.session_factory.begin() as session:
        session.add(
            ArtworkCache(
                cache_key=key,
                content_sha256=digest,
                mime_type="image/jpeg",
                width=10,
                height=10,
                relative_path=relative,
                status="ok",
            )
        )

    response = client.get(f"/artwork/{key}")

    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/jpeg"


def test_page_captures_event_cursor_before_reading_rendered_state(client, monkeypatch) -> None:
    _setup(client)
    client.app.state.events.emit("request", "request.preview", "Proposal ready")
    original_bounds = client.app.state.events.bounds
    original_search = client.app.state.library.search
    order: list[str] = []
    captured: list[int] = []

    def bounds() -> tuple[int | None, int | None]:
        order.append("cursor")
        result = cast(tuple[int | None, int | None], original_bounds())
        captured.append(result[1] or 0)
        return result

    def search(query: str, page: int, page_size: int) -> object:
        order.append("state")
        return original_search(query, page, page_size)

    monkeypatch.setattr(client.app.state.events, "bounds", bounds)
    monkeypatch.setattr(client.app.state.library, "search", search)

    response = client.get("/library")

    assert response.status_code == 200
    assert order[:2] == ["cursor", "state"]
    match = re.search(rb'data-event-cursor="(\d+)"', response.content)
    assert match is not None and int(match.group(1)) == captured[0]
