from __future__ import annotations

import json

from sqlalchemy import select

from app.db.models import (
    Conversation,
    EvidenceReference,
    Request,
    RequestTrack,
    SourceCandidate,
    User,
)
from app.services.security import SESSION_COOKIE


def _setup(client) -> None:
    page = client.get("/setup")
    response = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "correct horse battery staple",
            "csrf_token": page.cookies["music_agent_preauth"],
            "acknowledge_rights": "yes",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _seed_request(
    factory, user_id: str, suffix: str, *, request_scoped_source: bool = False
) -> str:
    with factory.begin() as session:
        conversation = Conversation(user_id=user_id, title="Exact recording presentation")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user_id,
            conversation_id=conversation.id,
            raw_text="find Tarantella by Gabry Ponte & KEL",
            action="find",
            input_kind="natural_language",
            requested_count=1,
            status="preview",
            idempotency_key=f"presentation-{suffix}",
            orchestration_attempt_id="11111111-1111-4111-8111-111111111111",
            model_rounds_used=2,
            configured_model_rounds=10,
            configured_tool_calls=10,
            configured_agent_seconds=120,
            termination_reason="normal_synthesis",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Gabry Ponte & KEL",
            title="Tarantella",
            album="Tarantella",
            album_artist="Gabry Ponte & KEL",
            year=2024,
            duration_seconds=146.0,
            recording_mbid="11111111-1111-1111-1111-111111111111",
            release_mbid="22222222-2222-2222-2222-222222222222",
            canonical_identity_verified=True,
            version_signature="studio",
            rationale="Exact recording identity and compatible duration.",
            selected=True,
            metadata_confidence=0.97,
            metadata_provenance_json=json.dumps(
                {
                    "source": "musicbrainz_search_recordings",
                    "request_constraints": {"requested_version": None},
                }
            ),
        )
        session.add(track)
        session.flush()
        evidence_id = None
        if request_scoped_source:
            evidence = EvidenceReference(
                request_id=request.id,
                request_track_id=None,
                job_id=None,
                provider="youtube",
                evidence_kind="provider_search_result",
                canonical_url=(
                    "https://www.youtube.com/watch?v=never-render-this-secret-source-url"
                ),
                provider_item_id=f"safe-source-{suffix}",
                status="available",
                sanitized_metadata_json="{}",
            )
            session.add(evidence)
            session.flush()
            evidence_id = evidence.id
            session.add(
                EvidenceReference(
                    request_id=request.id,
                    request_track_id=None,
                    job_id=None,
                    provider="automatic",
                    evidence_kind="source_search_diagnostics",
                    canonical_url=None,
                    provider_item_id=None,
                    status="available",
                    sanitized_metadata_json=json.dumps(
                        {
                            "discovery_diagnostic_runs": [
                                {
                                    "schema_version": 1,
                                    "query_variant_count": 3,
                                    "found_count": 9,
                                    "probed_count": 4,
                                    "accepted_count": 2,
                                    "query_attempts": [
                                        {
                                            "provider": "youtube",
                                            "query": (
                                                "Gabry Ponte & KEL Tarantella official audio"
                                            ),
                                            "found_count": 5,
                                        },
                                        {
                                            "provider": "youtube",
                                            "query": (
                                                "https://secret.invalid/NEVER_RENDER_QUERY_URL"
                                            ),
                                            "found_count": 1,
                                        },
                                    ],
                                    "rejection_counts": {
                                        "duplicate_source_id": 1,
                                        "probe_rejected": 2,
                                        "unrecognized_provider_reason": 999,
                                    },
                                    "stopped_early": True,
                                }
                            ]
                        }
                    ),
                )
            )
        session.add(
            SourceCandidate(
                evidence_id=evidence_id,
                request_track_id=None if request_scoped_source else track.id,
                provider="youtube",
                extractor="youtube",
                source_id=f"safe-source-{suffix}",
                acquisition_url=(
                    "https://www.youtube.com/watch?v=never-render-this-secret-source-url"
                ),
                provider_title="Gabry Ponte, KEL - Tarantella",
                provider_artist="Gabry Ponte, KEL",
                uploader="Third-party Archive",
                uploader_relationship="third_party",
                duration_seconds=146.0,
                version_signature="studio",
                group_key=f"tarantella-{suffix}",
                local_score=0.96,
                policy_status="allowed",
                probe_status="valid",
                contradictions_json="[]",
                sanitized_metadata_json=json.dumps(
                    {
                        "description": "NEVER_RENDER_UNTRUSTED_DESCRIPTION",
                        "webpage_url": "https://private.invalid/NEVER_RENDER_RAW_URL",
                        "http_headers": {"Authorization": "NEVER_RENDER_AUTH_TOKEN"},
                        "raw_query": "NEVER_RENDER_RAW_QUERY",
                        "discovery_diagnostics": (
                            None
                            if request_scoped_source
                            else {
                                "schema_version": 1,
                                "query_variant_count": 3,
                                "found_count": 9,
                                "probed_count": 4,
                                "accepted_count": 2,
                                "query_attempts": [
                                    {
                                        "provider": "youtube",
                                        "query": ("Gabry Ponte & KEL Tarantella official audio"),
                                        "found_count": 5,
                                    },
                                    {
                                        "provider": "youtube",
                                        "query": ("https://secret.invalid/NEVER_RENDER_QUERY_URL"),
                                        "found_count": 1,
                                    },
                                ],
                                "rejection_counts": {
                                    "duplicate_source_id": 1,
                                    "probe_rejected": 2,
                                    "unrecognized_provider_reason": 999,
                                },
                                "stopped_early": True,
                            }
                        ),
                        "ranking_facts": {
                            "canonical_match": 1.0,
                            "requested_version": 1.0,
                            "duration_compatibility": 0.98,
                            "audio_availability_quality": 0.9,
                            "uploader_relationship": 0.0,
                            "provider_reliability": 0.9,
                            "provider_preference": 1.0,
                            "version_match": True,
                            "duration_compatible": True,
                            "canonical_exact": True,
                            "contradiction_codes": ["UNKNOWN_UNTRUSTED_CONTRADICTION"],
                        },
                    }
                ),
            )
        )
        return request.id


def test_admin_preview_distinguishes_recording_release_and_source_without_raw_data(client):
    _setup(client)
    with client.app.state.session_factory() as session:
        admin_id = session.scalar(select(User.id).where(User.username_normalized == "admin"))
    assert admin_id is not None
    request_id = _seed_request(
        client.app.state.session_factory,
        admin_id,
        "admin",
        request_scoped_source=True,
    )

    page = client.get(f"/requests/{request_id}")
    assert page.status_code == 200
    rendered = page.text
    assert "Requested version:" in rendered
    assert "No special version (studio/original default)" in rendered
    assert "Resolved recording version:" in rendered
    assert "Studio · verified canonical match" in rendered
    assert "Selected Canonical Release:" in rendered
    assert "Tarantella (2024)" in rendered
    assert "Candidate acquisition source:" in rendered
    assert "YouTube · uploaded by Third-party Archive" in rendered
    assert "Safe matching diagnostics" in rendered
    assert "Discovery: 3 query variants · 9 found · 4 probed · 2 accepted" in rendered
    assert "Filtered: 1 duplicate source identities · 2 rejected probes" in rendered
    assert "YouTube search: Gabry Ponte &amp; KEL Tarantella official audio · 5 found" in rendered
    assert "YouTube search: [redacted unsafe query] · 1 found" in rendered
    assert "recording identity 100%" in rendered
    assert "Model execution diagnostics" in rendered
    assert "2 of 10 model rounds" in rendered

    for forbidden in (
        "never-render-this-secret-source-url",
        "NEVER_RENDER_UNTRUSTED_DESCRIPTION",
        "NEVER_RENDER_RAW_URL",
        "NEVER_RENDER_AUTH_TOKEN",
        "NEVER_RENDER_RAW_QUERY",
        "NEVER_RENDER_QUERY_URL",
        "UNKNOWN_UNTRUSTED_CONTRADICTION",
        "unrecognized_provider_reason",
    ):
        assert forbidden not in rendered

    api = client.get(f"/api/v1/requests/{request_id}")
    assert api.status_code == 200
    [track] = api.json()["tracks"]
    assert track["requested_version"] is None
    assert track["recording_version"] == {"value": "studio", "authority": "canonical"}
    assert track["release_context"] == {
        "album": "Tarantella",
        "year": 2024,
        "canonical": True,
    }
    assert "acquisition_url" not in json.dumps(api.json())


def test_matching_diagnostics_are_hidden_from_standard_users(client):
    _setup(client)
    with client.app.state.session_factory.begin() as session:
        user = User(
            username="listener",
            username_normalized="listener",
            password_hash="fixture",  # noqa: S106
            role="user",
            is_active=True,
        )
        session.add(user)
        session.flush()
        user_id = user.id
    request_id = _seed_request(
        client.app.state.session_factory,
        user_id,
        "listener",
        request_scoped_source=True,
    )
    login = client.app.state.auth.create_session(user_id)
    client.cookies.set(SESSION_COOKIE, login.token, path="/")

    page = client.get(f"/requests/{request_id}")
    assert page.status_code == 200
    assert "Requested version:" in page.text
    assert "Resolved recording version:" in page.text
    assert "Selected Canonical Release:" in page.text
    assert "Candidate acquisition source:" in page.text
    assert "Safe matching diagnostics" not in page.text
    assert "Model execution diagnostics" not in page.text
    assert "Discovery: 3 query variants" not in page.text
    assert "NEVER_RENDER" not in page.text
