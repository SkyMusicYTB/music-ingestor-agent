from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    ArtworkCache,
    Conversation,
    DownloadJob,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    User,
)
from app.main import create_app
from app.repositories.decisions import (
    DecisionSelection,
    apply_review_bundle,
    review_bundle_fingerprint,
)
from app.workers.queue import DownloadJobQueue, JobLease


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


def _active_review_job(client, suffix: str) -> tuple[str, JobLease]:
    token = f"review-lease-{suffix}"
    with client.app.state.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.username_normalized == "admin"))
        assert user is not None
        conversation = Conversation(user_id=user.id, title=f"Review {suffix}")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Yellow by Coldplay",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="queued",
            idempotency_key=f"review-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Coldplay",
            title="Yellow",
            selected=True,
        )
        session.add(track)
        session.flush()
        snapshot = {
            "request_track_id": track.id,
            "artist": "Coldplay",
            "title": "Yellow",
        }
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key=f"review:{suffix}",
            status="active",
            stage="resolving_metadata",
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        return job.id, JobLease(
            job_id=job.id,
            token=token,
            approved_snapshot=snapshot,
            retry_count=0,
        )


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


def test_private_network_accepts_missing_origin_with_valid_csrf(client) -> None:
    _setup(client)
    response = client.post(
        "/api/v1/requests",
        json={"text": "a song", "action": "find", "conversation_id": None},
        headers={
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
            "Idempotency-Key": "test-request-0003",
        },
    )
    assert response.status_code == 200

    null_origin = client.post(
        "/api/v1/requests",
        json={"text": "another song", "action": "find", "conversation_id": None},
        headers={
            "Origin": "null",
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
            "Idempotency-Key": "test-request-0003-null",
        },
    )
    assert null_origin.status_code == 200


def test_private_network_rejects_explicit_cross_site(client) -> None:
    _setup(client)
    response = client.post(
        "/api/v1/requests",
        json={"text": "a song", "action": "find", "conversation_id": None},
        headers={
            "Origin": "null",
            "Sec-Fetch-Site": "cross-site",
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
            "Idempotency-Key": "test-request-0004",
        },
    )
    assert response.status_code == 403


def test_strict_origin_policy_requires_matching_origin(settings, engine) -> None:
    strict = settings.model_copy(update={"origin_policy": "strict"})
    with TestClient(create_app(strict), client=("127.0.0.1", 5050)) as strict_client:
        _setup(strict_client)
        headers = {
            "X-CSRF-Token": strict_client.cookies["music_agent_csrf"],
            "Idempotency-Key": "test-request-strict-0001",
        }
        missing = strict_client.post(
            "/api/v1/requests",
            json={"text": "a song", "action": "find", "conversation_id": None},
            headers=headers,
        )
        assert missing.status_code == 403

        mismatched = strict_client.post(
            "/api/v1/requests",
            json={"text": "a song", "action": "find", "conversation_id": None},
            headers={**headers, "Origin": "http://evil.invalid"},
        )
        assert mismatched.status_code == 403

        matching_referer = strict_client.post(
            "/api/v1/requests",
            json={"text": "a song", "action": "find", "conversation_id": None},
            headers={
                **headers,
                "Idempotency-Key": "test-request-strict-0002",
                "Referer": "http://testserver/library?page=2",
            },
        )
        assert matching_referer.status_code == 200


def test_security_headers_are_present(client) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_security_boundaries_run_in_required_order(client) -> None:
    middleware = [item.cls.__name__ for item in client.app.user_middleware]
    assert middleware.index("TrustedProxyMiddleware") < middleware.index("AllowedClientMiddleware")
    assert middleware.index("AllowedClientMiddleware") < middleware.index("TrustedHostMiddleware")
    assert middleware.index("TrustedHostMiddleware") < middleware.index("BodyLimitMiddleware")


def test_trusted_proxy_normalizes_client_host_and_https(settings, engine) -> None:
    proxied = settings.model_copy(
        update={
            "trusted_proxy_cidrs": ["127.0.0.0/8"],
            "public_base_url": "https://music.example.test",
        }
    )
    headers = {
        "Host": "internal.invalid:8787",
        "X-Forwarded-For": "100.64.0.10",
        "X-Forwarded-Host": "music.example.test",
        "X-Forwarded-Proto": "https",
    }
    with TestClient(create_app(proxied), client=("127.0.0.1", 5050)) as proxy_client:
        response = proxy_client.get("/setup", headers=headers)
        assert response.status_code == 200
        assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert "Secure" in response.headers["set-cookie"]

        denied = proxy_client.get(
            "/health/live",
            headers={**headers, "X-Forwarded-For": "8.8.8.8"},
        )
        assert denied.status_code == 403

        malformed = proxy_client.get(
            "/health/live",
            headers={**headers, "X-Forwarded-For": "not-an-address"},
        )
        assert malformed.status_code == 400

        malformed_host = proxy_client.get(
            "/health/live",
            headers={**headers, "X-Forwarded-Host": "music.example.test,evil.invalid"},
        )
        assert malformed_host.status_code == 400

        public_fallback = proxy_client.get(
            "/setup",
            headers={
                "Host": "internal.invalid:8787",
                "X-Forwarded-For": "100.64.0.10",
            },
        )
        assert public_fallback.status_code == 200
        assert public_fallback.headers["strict-transport-security"] == "max-age=31536000"


def test_untrusted_forwarded_headers_are_ignored(settings, engine) -> None:
    direct = settings.model_copy(update={"trusted_proxy_cidrs": ["10.0.0.0/8"]})
    with TestClient(create_app(direct), client=("127.0.0.1", 5050)) as direct_client:
        response = direct_client.get(
            "/health/live",
            headers={
                "X-Forwarded-For": "8.8.8.8",
                "X-Forwarded-Host": "evil.invalid",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 200
        assert "strict-transport-security" not in response.headers

        invalid_host = direct_client.get("/health/live", headers={"Host": "evil.invalid"})
        assert invalid_host.status_code == 400


def test_trusted_host_supports_exact_ipv6_without_broadening(settings, engine) -> None:
    ipv6 = settings.model_copy(
        update={
            "allowed_client_cidrs": ["::1/128"],
            "trusted_hosts": ["[::1]"],
        }
    )
    with TestClient(create_app(ipv6), client=("::1", 5050)) as ipv6_client:
        accepted = ipv6_client.get("/health/live", headers={"Host": "[::1]:8787"})
        assert accepted.status_code == 200

        rejected = ipv6_client.get("/health/live", headers={"Host": "[::2]:8787"})
        assert rejected.status_code == 400


def test_html_and_static_assets_have_safe_cache_policies(client) -> None:
    page = client.get("/setup")
    assert page.headers["cache-control"] == "private, no-store"
    match = re.search(r'src="(/static/app\.js\?v=([0-9a-f]{64}))"', page.text)
    assert match is not None

    fingerprinted = client.get(match.group(1))
    assert fingerprinted.status_code == 200
    assert fingerprinted.headers["cache-control"] == "public, max-age=31536000, immutable"

    bare = client.get("/static/app.js")
    assert bare.status_code == 200
    assert bare.headers["cache-control"] == "no-cache"


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


def test_download_review_renders_and_submits_only_pending_decisions(client) -> None:
    _setup(client)
    job_id, first_lease = _active_review_job(client, "mixed-bundle")
    queue = DownloadJobQueue(client.app.state.session_factory)
    first_options = [
        {
            "kind": "canonical_metadata",
            "rank": 1,
            "recording_candidate_id": "rec-yellow",
            "release_candidate_id": "rel-parachutes",
            "artist": "Coldplay",
            "title": "Yellow",
            "album": "Parachutes",
            "score": 0.82,
        }
    ]
    assert queue.require_review(
        first_lease,
        reason="first release distinction",
        options=first_options,
    )
    with client.app.state.session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        first_decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.state == "pending",
            )
        )
        assert job is not None and first_decision is not None
        first_option = session.scalar(
            select(JobReviewOption).where(JobReviewOption.decision_id == first_decision.id)
        )
        assert first_option is not None
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=review_bundle_fingerprint([first_decision]),
            revision=job.decision_revision,
            selections=[
                DecisionSelection(
                    decision_id=first_decision.id,
                    option_id=first_option.id,
                )
            ],
        )
        selected_decision_id = first_decision.id
        selected_option_id = first_option.id
        second_token = "review-lease-mixed-second"  # noqa: S105 - inert fixture token
        job.status = "active"
        job.stage = "resolving_metadata"
        job.lease_token = second_token
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    second_options = [
        {
            "kind": "canonical_metadata",
            "rank": 1,
            "recording_candidate_id": "rec-yellow",
            "release_candidate_id": "rel-yellow-single",
            "artist": "Coldplay",
            "title": "Yellow",
            "album": "Yellow",
            "year": 2000,
            "version": "studio",
            "release_status": "Official",
            "primary_type": "Single",
            "score": 0.80,
        },
        {
            "kind": "canonical_metadata",
            "rank": 2,
            "recording_candidate_id": "rec-yellow-equivalent",
            "release_candidate_id": "rel-yellow-equivalent",
            "artist": "Coldplay",
            "title": "Equivalent option must stay collapsed",
            "album": "Yellow",
            "year": 2000,
            "version": "studio",
            "release_status": "Official",
            "primary_type": "Single",
            "materially_different": False,
            "score": 0.79,
        },
    ]
    assert queue.require_review(
        JobLease(
            job_id=job_id,
            token=second_token,
            approved_snapshot={"artist": "Coldplay", "title": "Yellow"},
            retry_count=0,
        ),
        reason="materially different release context",
        options=second_options,
    )
    with client.app.state.session_factory() as session:
        job = session.get(DownloadJob, job_id)
        pending = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.state == "pending",
            )
        )
        assert job is not None and pending is not None
        pending_option = session.scalar(
            select(JobReviewOption).where(JobReviewOption.decision_id == pending.id)
        )
        assert pending_option is not None
        fingerprint = review_bundle_fingerprint([pending])
        revision = job.decision_revision
        pending_decision_id = pending.id
        pending_option_id = pending_option.id

    page = client.get("/downloads")
    assert page.status_code == 200
    article = page.text.split(f'data-job-id="{job_id}"', 1)[1].split("</article>", 1)[0]
    assert f'data-decision-id="{pending_decision_id}"' in article
    assert f'data-decision-id="{selected_decision_id}"' not in article
    assert f'value="{pending_option_id}"' in article
    assert f'value="{selected_option_id}"' not in article
    assert "Version: studio" in article
    assert "Year: 2000" in article
    assert "Status: Official" in article
    assert "Type: Single" in article
    assert "Equivalent option must stay collapsed" not in article

    csrf_headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": client.cookies["music_agent_csrf"],
    }
    blocked_retry = client.post(f"/api/v1/jobs/{job_id}/retry", headers=csrf_headers)
    assert blocked_retry.status_code == 409
    reviewed = client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={
            "bundle_fingerprint": fingerprint,
            "revision": revision,
            "selections": [
                {
                    "decision_id": pending_decision_id,
                    "option_id": pending_option_id,
                    "correction": None,
                }
            ],
        },
        headers=csrf_headers,
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "queued"


def test_review_without_safe_options_offers_a_retry_recovery_path(client) -> None:
    _setup(client)
    job_id, lease = _active_review_job(client, "no-options")
    queue = DownloadJobQueue(client.app.state.session_factory)
    assert queue.require_review(
        lease,
        reason="No safe permitted source is currently available",
        options=[],
    )

    page = client.get("/downloads")
    assert page.status_code == 200
    article = page.text.split(f'data-job-id="{job_id}"', 1)[1].split("</article>", 1)[0]
    assert "No safe permitted source is currently available" in article
    assert 'data-action="retry"' in article
    assert "review-form" not in article

    retried = client.post(
        f"/api/v1/jobs/{job_id}/retry",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
        },
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "queued"
    with client.app.state.session_factory() as session:
        job = session.get(DownloadJob, job_id)
        pending = list(
            session.scalars(
                select(JobDecision).where(
                    JobDecision.job_id == job_id,
                    JobDecision.state == "pending",
                )
            )
        )
        assert job is not None and job.error_code is None
        assert json.loads(job.warnings_json) == []
        assert len(pending) == 1
        assert (
            session.scalar(
                select(JobReviewOption.id).where(JobReviewOption.decision_id == pending[0].id)
            )
            is None
        )

    cancelled_job_id, cancelled_lease = _active_review_job(client, "no-options-cancel")
    assert queue.require_review(
        cancelled_lease,
        reason="No safe source remains after probing",
        options=[],
    )
    cancelled = client.post(
        f"/api/v1/jobs/{cancelled_job_id}/cancel",
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
        },
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
