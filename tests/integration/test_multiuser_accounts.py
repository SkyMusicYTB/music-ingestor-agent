from __future__ import annotations

import json
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.accounts import AccountProfile, PasswordResponse, UserPageResponse
from app.db.ids import uuid7
from app.db.models import (
    Conversation,
    DownloadJob,
    Event,
    EvidenceReference,
    OpenAICall,
    Request,
    RequestTrack,
    Track,
    User,
)
from app.main import create_app
from app.tools.media_sources import build_media_source_tools, media_tool_authorization

PASSWORD = "synthetic-multiuser-password"  # noqa: S105
NEW_PASSWORD = "changed-multiuser-password"  # noqa: S105


def _headers(client, **extra):
    return {"X-CSRF-Token": client.cookies["music_agent_csrf"], **extra}


def _setup(client):
    token = client.get("/setup").cookies["music_agent_preauth"]
    result = client.post(
        "/setup",
        data={
            "username": "owner",
            "password": PASSWORD,
            "csrf_token": token,
            "acknowledge_rights": "yes",
        },
        follow_redirects=False,
    )
    assert result.status_code == 303


def _login(client, username, password=PASSWORD):
    token = client.get("/login", follow_redirects=False).cookies["music_agent_preauth"]
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        follow_redirects=False,
    )


def _reauth(client):
    result = client.post(
        "/api/v1/admin/reauthenticate",
        json={"current_password": PASSWORD},
        headers=_headers(client),
    )
    assert result.status_code == 200, result.text


@pytest.fixture
def people(client, settings):
    _setup(client)
    clients = {"owner": client}
    ids = {"owner": client.get("/api/v1/account").json()["id"]}
    with ExitStack() as stack:
        for username in ("alice", "bob"):
            created = client.post(
                "/api/v1/admin/users",
                json={
                    "username": username,
                    "temporary_password": PASSWORD,
                    "must_change_password": False,
                },
                headers=_headers(client),
            )
            assert created.status_code == 201, created.text
            ids[username] = created.json()["user_id"]
            browser = stack.enter_context(
                TestClient(create_app(settings), client=("127.0.0.1", 5050))
            )
            assert _login(browser, username).status_code == 303
            clients[username] = browser
        yield clients, ids


def _activity(factory, user_id, label, status="failed"):
    with factory.begin() as session:
        conversation = Conversation(user_id=user_id, title=f"private-conversation-{label}")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user_id,
            conversation_id=conversation.id,
            raw_text=f"private-request-{label}",
            action="add",
            status="preview",
            idempotency_key=f"test-isolation-{uuid7()}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Fixture Artist",
            title=f"Private title {label}",
            selected=True,
        )
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(
                {"artist": track.artist, "title": track.title, "request_track_id": track.id}
            ),
            dedup_key=f"isolation-{uuid7()}",
            status=status,
            error_code="private_error",
            error_message=f"private-provider-error-{label}",
        )
        session.add(job)
        session.flush()
        evidence = EvidenceReference(
            request_id=request.id,
            request_track_id=track.id,
            job_id=job.id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=fixture12345",
            status="available",
        )
        session.add(evidence)
        call = OpenAICall(
            request_id=request.id,
            owner_user_id=user_id,
            model="fixture-model",
            prompt_version="fixture-v1",
            prompt_hash="0" * 64,
            status="completed",
            usage_reported=True,
            total_tokens=100,
            input_tokens=60,
            output_tokens=40,
            estimated_cost_microusd=3,
        )
        session.add(call)
        session.flush()
        return {
            "conversation": conversation.id,
            "request": request.id,
            "track": track.id,
            "job": job.id,
            "call": call.id,
            "evidence": evidence.id,
        }


def test_generated_password_is_one_time_and_forced_change_works(client, settings, session_factory):
    _setup(client)
    created = client.post(
        "/api/v1/admin/users", json={"username": "temporary"}, headers=_headers(client)
    )
    assert created.status_code == 201, created.text
    temporary = created.json()["temporary_password"]
    assert len(temporary) >= 20
    assert "no-store" in created.headers["cache-control"]
    listed = client.get("/api/v1/admin/users")
    assert temporary not in listed.text
    assert "password_hash" not in listed.text
    assert "token_hash" not in listed.text
    assert "csrf_hash" not in listed.text
    assert temporary not in client.get("/admin/users").text
    with session_factory() as session:
        assert all(temporary not in row.details_json for row in session.scalars(select(Event)))
    with TestClient(create_app(settings), client=("127.0.0.1", 5050)) as browser:
        login = _login(browser, "temporary", temporary)
        assert login.status_code == 303
        assert login.headers["location"] == "/account/change-password"
        old_cookie = browser.cookies["music_agent_session"]
        old_csrf = browser.cookies["music_agent_csrf"]
        for path in (
            "/api/v1/account",
            "/api/v1/library",
            "/api/v1/usage",
            "/api/v1/jobs",
            "/api/v1/events",
        ):
            blocked = browser.get(path)
            assert blocked.status_code == 403
            assert blocked.json()["detail"]["code"] == "password_change_required"
        page = browser.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/account/change-password"
        forced = browser.get("/account/change-password")
        assert forced.status_code == 200
        assert 'href="/admin/users"' not in forced.text
        assert "no-store" in forced.headers["cache-control"]
        changed = browser.post(
            "/api/v1/account/change-password",
            json={"new_password": NEW_PASSWORD, "confirmation": NEW_PASSWORD},
            headers=_headers(browser),
        )
        assert changed.status_code == 200, changed.text
        assert browser.cookies["music_agent_session"] != old_cookie
        assert browser.cookies["music_agent_csrf"] != old_csrf
        assert browser.app.state.auth.resolve_session(old_cookie) is None
        assert browser.get("/api/v1/account").json()["must_change_password"] is False


def test_account_response_schemas_are_explicit_and_cannot_serialize_hashes():
    for model in (AccountProfile, PasswordResponse, UserPageResponse):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        encoded = json.dumps(schema)
        for forbidden in ("password_hash", "token_hash", "csrf_hash", "key_hash"):
            assert forbidden not in encoded
    response = PasswordResponse(
        user_id=uuid7(), temporary_password=PASSWORD, password_visible_once=True
    )
    assert PASSWORD not in repr(response)
    assert response.model_dump()["temporary_password"] == PASSWORD


def test_forced_change_still_allows_logout(client, settings):
    _setup(client)
    created = client.post(
        "/api/v1/admin/users",
        json={"username": "temporary", "temporary_password": PASSWORD},
        headers=_headers(client),
    )
    assert created.status_code == 201
    with TestClient(create_app(settings), client=("127.0.0.1", 5050)) as browser:
        assert _login(browser, "temporary").status_code == 303
        logout = browser.post(
            "/logout",
            data={"csrf_token": browser.cookies["music_agent_csrf"]},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert "music_agent_session" not in browser.cookies


@pytest.mark.parametrize(
    "path",
    [
        "/admin/users",
        "/settings",
        "/health",
        "/api/v1/admin/users",
        "/api/v1/health",
        "/api/v1/library/scans",
        "/api/v1/library/audit",
        "/api/v1/usage?scope=all",
        "/api/v1/usage?scope=system",
        "/api/v1/usage/by-user",
    ],
)
def test_standard_user_cannot_access_admin_views(people, path):
    clients, _ids = people
    response = clients["alice"].get(path)
    assert response.status_code == 403, (path, response.text)


def test_pagination_rejects_sqlite_integer_overflow(people):
    clients, _ids = people
    for path in (
        "/downloads",
        "/api/v1/jobs",
        "/library",
        "/api/v1/library",
        "/api/v1/library/scans",
        "/api/v1/admin/users",
    ):
        response = clients["owner"].get(path, params={"page": str(1 << 64)})
        assert response.status_code == 422, (path, response.text)


def test_admin_mutations_return_consistent_403_for_standard_users(people):
    clients, ids = people
    browser = clients["alice"]
    for target in (ids["bob"], uuid7()):
        for suffix in ("reset-password", "revoke-sessions"):
            assert (
                browser.post(
                    f"/api/v1/admin/users/{target}/{suffix}", json={}, headers=_headers(browser)
                ).status_code
                == 403
            )
        assert (
            browser.patch(
                f"/api/v1/admin/users/{target}",
                json={"is_active": False},
                headers=_headers(browser),
            ).status_code
            == 403
        )
    assert (
        browser.post(
            "/api/v1/admin/users", json={"username": "forbidden"}, headers=_headers(browser)
        ).status_code
        == 403
    )
    assert browser.post("/api/v1/library/rescan", headers=_headers(browser)).status_code == 403


def test_account_mutations_require_csrf_correct_type_and_bounded_strict_bodies(people):
    clients, ids = people
    browser = clients["owner"]
    assert browser.post("/api/v1/admin/users", json={"username": "csrf-failure"}).status_code == 403
    assert (
        browser.post(
            "/api/v1/admin/users",
            json={"username": "cross-site"},
            headers=_headers(browser, **{"Sec-Fetch-Site": "cross-site"}),
        ).status_code
        == 403
    )
    assert (
        browser.post(
            "/api/v1/admin/users",
            content='{"username":"wrong-type"}',
            headers=_headers(browser, **{"Content-Type": "text/plain"}),
        ).status_code
        == 415
    )
    assert (
        browser.post(
            "/api/v1/admin/users",
            json={"username": "extra", "password_hash": "forbidden"},
            headers=_headers(browser),
        ).status_code
        == 422
    )
    marker = "never-echo-this-password"
    invalid = browser.post(
        "/api/v1/admin/users",
        json={"username": "bad", "temporary_password": {"secret": marker}},
        headers=_headers(browser),
    )
    assert invalid.status_code == 422
    assert marker not in invalid.text
    assert "input" not in invalid.text
    bounded = browser.post(
        "/api/v1/admin/users",
        content=json.dumps({"username": "limit"}) + " " * 16384,
        headers=_headers(browser, **{"Content-Type": "application/json"}),
    )
    assert bounded.status_code == 413
    assert (
        browser.patch(
            f"/api/v1/admin/users/{ids['bob']}",
            json={"is_active": "false"},
            headers=_headers(browser),
        ).status_code
        == 422
    )


def test_reauth_is_explicit_and_temp_reset_response_is_not_replayable(people):
    clients, ids = people
    browser = clients["owner"]
    target = ids["bob"]
    reset = browser.post(
        f"/api/v1/admin/users/{target}/reset-password", json={}, headers=_headers(browser)
    )
    assert reset.status_code == 403
    assert reset.json()["detail"]["code"] == "reauthentication_required"
    _reauth(browser)
    reset = browser.post(
        f"/api/v1/admin/users/{target}/reset-password", json={}, headers=_headers(browser)
    )
    assert reset.status_code == 200
    password = reset.json()["temporary_password"]
    assert "no-store" in reset.headers["cache-control"]
    assert password not in browser.get("/api/v1/admin/users").text
    assert clients["bob"].get("/api/v1/account").status_code == 401


def test_admin_create_role_requires_reauth_standard_creation_does_not(people):
    clients, _ids = people
    browser = clients["owner"]
    denied = browser.post(
        "/api/v1/admin/users",
        json={"username": "new-admin", "role": "admin"},
        headers=_headers(browser),
    )
    assert denied.status_code == 403
    _reauth(browser)
    allowed = browser.post(
        "/api/v1/admin/users",
        json={"username": "new-admin", "role": "admin"},
        headers=_headers(browser),
    )
    assert allowed.status_code == 201
    duplicate = browser.post(
        "/api/v1/admin/users", json={"username": " Alice "}, headers=_headers(browser)
    )
    assert duplicate.status_code == 409


def test_admin_and_standard_user_navigation(people):
    clients, _ids = people
    regular = clients["alice"].get("/account").text
    assert 'href="/account"' in regular
    assert 'href="/admin/users"' not in regular
    assert 'href="/settings"' not in regular
    owner = clients["owner"].get("/admin/users")
    assert owner.status_code == 200, owner.text
    assert 'href="/admin/users"' in owner.text
    assert "data-account-action" in owner.text
    assert "accounts.js?v=" in owner.text


def test_user_list_pagination_and_safe_totals(people, session_factory):
    clients, ids = people
    browser = clients["owner"]
    _activity(session_factory, ids["alice"], "alice")
    result = browser.get("/api/v1/admin/users?page_size=25&q=alice")
    assert result.status_code == 200
    data = result.json()
    assert data["total"] == 1
    account = data["items"][0]
    assert account["username"] == "alice"
    assert account["request_count"] == 1
    assert account["download_count"] == 1
    assert account["usage"]["tokens"] == 100
    assert "private-request-alice" not in result.text
    assert "private-provider-error-alice" not in result.text
    assert browser.get("/api/v1/admin/users?page_size=26").status_code == 422
    assert browser.get("/api/v1/admin/users?page=2").json()["items"] == []


def test_all_request_and_conversation_boundaries(people, session_factory):
    clients, ids = people
    owner = _activity(session_factory, ids["alice"], "alice")
    foreign = _activity(session_factory, ids["bob"], "bob")
    browser = clients["alice"]
    assert browser.get(f"/api/v1/requests/{owner['request']}").status_code == 200
    for actor in (clients["alice"], clients["owner"]):
        assert actor.get(f"/requests/{foreign['request']}").status_code == 404
        response = actor.get(f"/api/v1/requests/{foreign['request']}")
        assert response.status_code == 404
        assert "private-request-bob" not in response.text
        assert (
            actor.post(
                f"/api/v1/requests/{foreign['request']}/refinements",
                json={"text": "more"},
                headers=_headers(actor, **{"Idempotency-Key": f"foreign-refine-{uuid7()}"}),
            ).status_code
            == 404
        )
        assert (
            actor.post(
                f"/api/v1/requests/{foreign['request']}/approval",
                json={"track_ids": [foreign["track"]], "acknowledge_rights": True},
                headers=_headers(actor),
            ).status_code
            == 404
        )
        create = actor.post(
            "/api/v1/requests",
            json={
                "text": "add a song",
                "action": "add",
                "conversation_id": foreign["conversation"],
            },
            headers=_headers(actor, **{"Idempotency-Key": f"foreign-conversation-{uuid7()}"}),
        )
        assert create.status_code == 400
    mismatched_track = browser.post(
        f"/api/v1/requests/{owner['request']}/approval",
        json={"track_ids": [foreign["track"]], "acknowledge_rights": True},
        headers=_headers(browser),
    )
    assert mismatched_track.status_code == 409


def test_job_mutations_and_reviews_never_cross_user_boundaries(people, session_factory):
    clients, ids = people
    alice = _activity(session_factory, ids["alice"], "alice")
    bob = _activity(session_factory, ids["bob"], "bob")
    admin = _activity(session_factory, ids["owner"], "owner")
    for name, own in (("alice", alice), ("bob", bob), ("owner", admin)):
        browser = clients[name]
        response = browser.get("/api/v1/jobs?page_size=25")
        assert response.status_code == 200, response.text
        assert {row["id"] for row in response.json()["jobs"]} == {own["job"]}
        foreign = bob if name != "bob" else alice
        for action in ("retry", "cancel", "dismiss", "restore"):
            assert (
                browser.post(
                    f"/api/v1/jobs/{foreign['job']}/{action}", headers=_headers(browser)
                ).status_code
                == 404
            )
        assert (
            browser.post(
                f"/api/v1/jobs/{foreign['job']}/review",
                json={
                    "bundle_fingerprint": "a" * 64,
                    "revision": 1,
                    "selections": [{"decision_id": uuid7(), "option_id": uuid7()}],
                },
                headers=_headers(browser),
            ).status_code
            == 404
        )
        assert (
            "private-provider-error-" + ("bob" if name != "bob" else "alice")
            not in browser.get("/downloads").text
        )


def test_clear_finished_is_own_only_for_admin_and_preserves_other_activity(people, session_factory):
    clients, ids = people
    rows = {name: _activity(session_factory, user_id, name) for name, user_id in ids.items()}
    active = _activity(session_factory, ids["owner"], "active", status="queued")
    browser = clients["owner"]
    result = browser.post("/api/v1/jobs/clear-finished", json={}, headers=_headers(browser))
    assert result.json() == {"dismissed": 1}
    assert browser.post(
        "/api/v1/jobs/clear-finished", json={}, headers=_headers(browser)
    ).json() == {"dismissed": 0}
    with session_factory() as session:
        assert session.get(DownloadJob, rows["owner"]["job"]).dismissed_at is not None
        assert session.get(DownloadJob, rows["alice"]["job"]).dismissed_at is None
        assert session.get(DownloadJob, rows["bob"]["job"]).dismissed_at is None
        assert session.get(DownloadJob, active["job"]).dismissed_at is None
        assert session.scalar(select(func.count()).select_from(Request)) == 4
        assert session.scalar(select(func.count()).select_from(OpenAICall)) == 4
    hidden = browser.get("/api/v1/jobs?view=hidden").json()["jobs"]
    assert [row["id"] for row in hidden] == [rows["owner"]["job"]]
    assert (
        browser.post(
            f"/api/v1/jobs/{rows['owner']['job']}/restore", headers=_headers(browser)
        ).status_code
        == 200
    )
    assert (
        browser.post(
            f"/api/v1/jobs/{rows['owner']['job']}/restore", headers=_headers(browser)
        ).status_code
        == 200
    )


def test_usage_owner_aggregate_and_system_totals_reconcile(people, session_factory):
    clients, ids = people
    activities = {name: _activity(session_factory, user_id, name) for name, user_id in ids.items()}
    with session_factory.begin() as session:
        session.add(
            OpenAICall(
                model="system-model",
                prompt_version="system",
                prompt_hash="f" * 64,
                status="completed",
                usage_reported=True,
                total_tokens=25,
                estimated_cost_microusd=1,
            )
        )
    for name, browser in clients.items():
        result = browser.get("/api/v1/usage").json()
        assert result["daily"][0]["total_tokens"] == 100
        assert {row["request_id"] for row in result["recent"]} == {activities[name]["request"]}
        for other in set(clients) - {name}:
            assert activities[other]["call"] not in json.dumps(result)
    all_users = clients["owner"].get("/api/v1/usage?scope=all").json()
    system = clients["owner"].get("/api/v1/usage?scope=system").json()
    assert all_users["daily"][0]["total_tokens"] == 325
    assert system["daily"][0]["total_tokens"] == 25
    assert all_users["daily"][0]["estimated_cost_microusd"] == 10
    groups = clients["owner"].get("/api/v1/usage/by-user").json()["groups"]
    assert sum(group["known_total_tokens"] for group in groups) == 325


def test_shared_library_is_visible_without_request_provenance(people, session_factory):
    clients, _ids = people
    with session_factory.begin() as session:
        track = Track(
            artist="Shared Artist",
            artist_normalized="shared artist",
            title="Shared music",
            title_normalized="shared music",
            filepath="Shared Artist/Album/shared.flac",
            codec="flac",
            file_size=100,
            file_mtime_ns=123,
            is_present=True,
        )
        session.add(track)
        session.flush()
        track_id = track.id
    for browser in clients.values():
        page = browser.get("/api/v1/library")
        assert page.status_code == 200
        assert any(item["id"] == track_id for item in page.json()["items"])
        assert "requested_by" not in page.text
        assert "job_id" not in page.text
    assert clients["owner"].post("/api/v1/library/rescan").status_code == 403
    assert (
        clients["owner"]
        .post("/api/v1/library/rescan", headers=_headers(clients["owner"]))
        .status_code
        == 202
    )


def test_deactivating_user_preserves_approved_jobs_and_revokes_access(people, session_factory):
    clients, ids = people
    queued = _activity(session_factory, ids["alice"], "queued", status="queued")
    running = _activity(session_factory, ids["alice"], "active", status="active")
    browser = clients["owner"]
    _reauth(browser)
    result = browser.patch(
        f"/api/v1/admin/users/{ids['alice']}", json={"is_active": False}, headers=_headers(browser)
    )
    assert result.status_code == 200
    assert clients["alice"].get("/api/v1/account").status_code == 401
    with session_factory() as session:
        assert session.get(DownloadJob, queued["job"]).status == "queued"
        assert session.get(DownloadJob, running["job"]).status == "active"
        assert session.get(User, ids["alice"]).is_active is False


@pytest.mark.asyncio
async def test_finite_media_tools_reject_foreign_request_and_evidence_ids(people, session_factory):
    _clients, ids = people
    own = _activity(session_factory, ids["alice"], "alice")
    foreign = _activity(session_factory, ids["bob"], "bob")
    search, probe = build_media_source_tools(session_factory)
    with media_tool_authorization(ids["alice"], own["request"]):
        with pytest.raises(ValueError, match="intent_id"):
            await search.handler(
                {"intent_id": foreign["request"], "provider": "youtube", "limit": 1}
            )
        with pytest.raises(ValueError, match="evidence_id"):
            await probe.handler({"evidence_id": foreign["evidence"]})
