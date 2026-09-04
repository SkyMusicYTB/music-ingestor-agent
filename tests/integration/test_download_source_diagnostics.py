from __future__ import annotations

import json

from sqlalchemy import delete, select

from app.db.models import (
    Conversation,
    DownloadJob,
    EvidenceReference,
    JobDecision,
    Request,
    RequestTrack,
    SourceCandidate,
    User,
)
from app.services.security import SESSION_COOKIE


def _setup(client) -> str:
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
    with client.app.state.session_factory() as session:
        user_id = session.scalar(select(User.id).where(User.username_normalized == "admin"))
    assert user_id is not None
    return user_id


def _seed_auto_queued_job(
    factory,
    user_id: str,
    *,
    suffix: str,
    status: str,
    stage: str,
) -> str:
    with factory.begin() as session:
        conversation = Conversation(user_id=user_id, title=f"Diagnostic {suffix}")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user_id,
            conversation_id=conversation.id,
            raw_text="add Tarantella by Gabry Ponte & KEL",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="auto_queued",
            idempotency_key=f"diagnostic-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Gabry Ponte & KEL",
            title="Tarantella",
            album="Tarantella",
            year=2024,
            duration_seconds=146.0,
            recording_mbid="11111111-1111-1111-1111-111111111111",
            canonical_identity_verified=True,
            version_signature="studio",
            rationale="Exact recording match",
            selected=True,
            metadata_confidence=0.98,
        )
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(
                {
                    "request_track_id": track.id,
                    "artist": track.artist,
                    "title": track.title,
                    "album": track.album,
                    "year": track.year,
                    "duration_seconds": track.duration_seconds,
                    "version_signature": "studio",
                    "canonical_identity_verified": True,
                }
            ),
            dedup_key=f"diagnostic:{suffix}",
            status=status,
            stage=stage,
            progress=1.0 if status == "completed" else 0.3 if status == "active" else 0.0,
            decision_revision=2,
            final_relative_path=(
                f"Gabry Ponte & KEL/Tarantella (2024)/01 - Tarantella-{suffix}.opus"
                if status == "completed"
                else None
            ),
        )
        session.add(job)
        session.flush()
        selected = SourceCandidate(
            request_track_id=track.id,
            job_id=job.id,
            provider="youtube",
            extractor="youtube",
            source_id=f"NEVER_RENDER_SELECTED_SOURCE_ID_{suffix}",
            acquisition_url=(f"https://www.youtube.com/watch?v=NEVER_RENDER_SELECTED_URL_{suffix}"),
            provider_title="NEVER_RENDER_PROVIDER_TITLE",
            provider_artist="Gabry Ponte, KEL",
            uploader='<img src=x onerror="NEVER_RENDER_UPLOADER_EVENT">Archive',
            uploader_relationship="third_party",
            duration_seconds=146.0,
            version_signature="studio",
            group_key=f"selected-{suffix}",
            local_score=0.96,
            policy_status="allowed",
            probe_status="valid",
            contradictions_json="[]",
            sanitized_metadata_json=json.dumps(
                {
                    "description_untrusted": "NEVER_RENDER_DESCRIPTION",
                    "http_headers": {"Authorization": "NEVER_RENDER_AUTHORIZATION"},
                    "provider_payload": {"cookie": "NEVER_RENDER_COOKIE"},
                    "discovery_diagnostics": {
                        "schema_version": 1,
                        "query_variant_count": 2,
                        "found_count": 6,
                        "probed_count": 3,
                        "accepted_count": 2,
                        "rejection_counts": {"duplicate_source_id": 1},
                        "stopped_early": True,
                        "query_attempts": [
                            {
                                "provider": "youtube",
                                "query": "Tarantella <script>NEVER_EXECUTE_SCRIPT</script>",
                                "found_count": 6,
                            },
                            {
                                "provider": "soundcloud",
                                "query": (
                                    "https://private.invalid/?token=NEVER_RENDER_QUERY_CREDENTIAL"
                                ),
                                "found_count": 0,
                            },
                            {
                                "provider": "malicious-provider",
                                "query": "NEVER_RENDER_UNSUPPORTED_PROVIDER_QUERY",
                                "found_count": 1,
                            },
                        ],
                    },
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
                    },
                }
            ),
        )
        rejected = SourceCandidate(
            request_track_id=track.id,
            job_id=job.id,
            provider="soundcloud",
            extractor="soundcloud",
            source_id=f"NEVER_RENDER_REJECTED_SOURCE_ID_{suffix}",
            acquisition_url=f"https://soundcloud.com/NEVER_RENDER_REJECTED_URL_{suffix}",
            provider_title="Tarantella (Remix)",
            provider_artist="Different Artist",
            uploader="Rejected uploader",
            uploader_relationship="unknown",
            duration_seconds=190.0,
            version_signature="remix",
            group_key=f"rejected-{suffix}",
            local_score=0.41,
            policy_status="exhausted",
            probe_status="valid",
            contradictions_json=json.dumps(
                ["unrequested_remix", "NEVER_RENDER_UNKNOWN_CONTRADICTION"]
            ),
            sanitized_metadata_json=json.dumps({"raw_error": "NEVER_RENDER_RAW_PROVIDER_ERROR"}),
            failure_code="media_validation_error",
        )
        conflicting = SourceCandidate(
            request_track_id=track.id,
            job_id=job.id,
            provider="youtube",
            extractor="youtube",
            source_id=f"conflicting-source-{suffix}",
            acquisition_url=f"https://www.youtube.com/watch?v=conflict{suffix}",
            provider_title="https://private.invalid/?token=NEVER_RENDER_PROVIDER_TITLE_TOKEN",
            provider_artist="Gabry Ponte & KEL",
            uploader="Festival Archive",
            uploader_relationship="third_party",
            duration_seconds=151.0,
            version_signature="live",
            group_key=f"conflicting-{suffix}",
            local_score=0.83,
            policy_status="allowed",
            probe_status="valid",
            contradictions_json=json.dumps(["unrequested_live"]),
            sanitized_metadata_json="{}",
        )
        session.add_all([selected, rejected, conflicting])
        session.flush()
        job.active_source_candidate_id = selected.id
        session.add_all(
            [
                JobDecision(
                    job_id=job.id,
                    category="acquisition_source",
                    candidate_set_fingerprint=("a" * 63) + suffix[-1],
                    revision=1,
                    state="selected",
                    selected_payload_json=json.dumps({"source_candidate_id": selected.id}),
                    decided_by="deterministic",
                    local_confidence=0.96,
                    reason_codes_json=json.dumps(
                        ["local_auto_match", "NEVER_RENDER_UNKNOWN_DECISION_REASON"]
                    ),
                ),
                JobDecision(
                    job_id=job.id,
                    category="acquisition_source",
                    candidate_set_fingerprint=("b" * 63) + suffix[-1],
                    revision=2,
                    state="rejected",
                    selected_payload_json=json.dumps({"source_candidate_id": rejected.id}),
                    decided_by="deterministic",
                    local_confidence=0.41,
                    reason_codes_json=json.dumps(
                        [
                            "source_contradiction",
                            "source_failed:media_validation_error",
                            "NEVER_RENDER_SECRET_REASON",
                        ]
                    ),
                ),
            ]
        )
        return job.id


def test_admin_downloads_show_bounded_source_diagnostics_for_every_job_state(client):
    admin_id = _setup(client)
    job_ids = [
        _seed_auto_queued_job(
            client.app.state.session_factory,
            admin_id,
            suffix=suffix,
            status=status,
            stage=stage,
        )
        for suffix, status, stage in (
            ("q", "queued", "queued"),
            ("a", "active", "downloading"),
            ("c", "completed", "completed"),
        )
    ]

    page = client.get("/downloads")
    assert page.status_code == 200
    assert page.text.count("Source resolution diagnostics") == 3
    for job_id in job_ids:
        article = page.text.split(f'data-job-id="{job_id}"', 1)[1].split("</article>", 1)[0]
        assert "Bounded source searches" in article
        assert (
            "YouTube search: Tarantella &lt;script&gt;NEVER_EXECUTE_SCRIPT&lt;/script&gt;"
            in article
        )
        assert "SoundCloud search: [redacted unsafe query] · 0 found" in article
        assert "Candidate 1 · YouTube</strong> · Selected · policy allowed · probe valid" in article
        assert "96% local score" in article
        assert "selected by deterministic source ranking" in article
        assert "NEVER_RENDER_PROVIDER_TITLE" in article
        assert "Reference:</strong> <code>" in article
        assert "Duration:</strong> 146s" in article
        assert "Candidate 2 · YouTube</strong> · Conflicting candidate" in article
        assert "[redacted unsafe provider text]" in article
        assert (
            "Candidate 3 · SoundCloud</strong> · Rejected · attempt exhausted · probe valid"
            in article
        )
        assert "unrequested remix" in article
        assert "<code>unrequested_remix</code>" in article
        assert "Conflicting candidate" in article
        assert "<code>unrequested_live</code>" in article
        assert "the downloaded media failed technical validation" in article
        assert "&lt;img src=x onerror=&#34;NEVER_RENDER_UPLOADER_EVENT&#34;&gt;Archive" in article
        assert '<img src=x onerror="NEVER_RENDER_UPLOADER_EVENT">' not in article

    for forbidden in (
        "NEVER_RENDER_SELECTED_SOURCE_ID",
        "NEVER_RENDER_REJECTED_SOURCE_ID",
        "NEVER_RENDER_SELECTED_URL",
        "NEVER_RENDER_REJECTED_URL",
        "NEVER_RENDER_DESCRIPTION",
        "NEVER_RENDER_PROVIDER_TITLE_TOKEN",
        "NEVER_RENDER_AUTHORIZATION",
        "NEVER_RENDER_COOKIE",
        "NEVER_RENDER_QUERY_CREDENTIAL",
        "NEVER_RENDER_UNSUPPORTED_PROVIDER_QUERY",
        "NEVER_RENDER_UNKNOWN_CONTRADICTION",
        "NEVER_RENDER_RAW_PROVIDER_ERROR",
        "NEVER_RENDER_UNKNOWN_DECISION_REASON",
        "NEVER_RENDER_SECRET_REASON",
    ):
        assert forbidden not in page.text

    with client.app.state.session_factory() as session:
        assert set(
            session.scalars(select(Request.status).where(Request.user_id == admin_id)).all()
        ) == {"auto_queued"}
    fragment = client.get("/downloads?fragment=true")
    assert fragment.status_code == 200
    assert fragment.text.count("Source resolution diagnostics") == 3
    assert "NEVER_RENDER_QUERY_CREDENTIAL" not in fragment.text


def test_admin_downloads_retain_central_diagnostics_when_no_candidate_survives(client):
    admin_id = _setup(client)
    job_id = _seed_auto_queued_job(
        client.app.state.session_factory,
        admin_id,
        suffix="empty",
        status="failed",
        stage="failed",
    )
    with client.app.state.session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        track = session.get(RequestTrack, job.request_track_id)
        assert track is not None
        job.active_source_candidate_id = None
        session.flush()
        session.execute(delete(JobDecision).where(JobDecision.job_id == job_id))
        session.execute(delete(SourceCandidate).where(SourceCandidate.job_id == job_id))
        session.add(
            EvidenceReference(
                request_id=track.request_id,
                request_track_id=job.request_track_id,
                job_id=job.id,
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
                                "query_variant_count": 1,
                                "found_count": 0,
                                "probed_count": 0,
                                "accepted_count": 0,
                                "rejection_counts": {"provider_search_rejected": 1},
                                "query_attempts": [
                                    {
                                        "provider": "youtube",
                                        "query": "Gabry Ponte KEL Tarantella",
                                        "found_count": 0,
                                    }
                                ],
                            }
                        ]
                    }
                ),
            )
        )

    page = client.get("/downloads")
    assert page.status_code == 200
    article = page.text.split(f'data-job-id="{job_id}"', 1)[1].split("</article>", 1)[0]
    assert "Source resolution diagnostics" in article
    assert "YouTube search: Gabry Ponte KEL Tarantella · 0 found" in article
    assert "No source candidate survived the bounded search and probe checks." in article
    assert "provider searches rejected by policy" in article


def test_download_source_diagnostics_apply_candidate_limit_per_job(client):
    admin_id = _setup(client)
    crowded_job_id = _seed_auto_queued_job(
        client.app.state.session_factory,
        admin_id,
        suffix="crowded",
        status="queued",
        stage="queued",
    )
    later_job_id = _seed_auto_queued_job(
        client.app.state.session_factory,
        admin_id,
        suffix="later",
        status="queued",
        stage="queued",
    )
    with client.app.state.session_factory.begin() as session:
        crowded_job = session.get(DownloadJob, crowded_job_id)
        later_job = session.get(DownloadJob, later_job_id)
        assert crowded_job is not None and later_job is not None
        later_selected = session.get(SourceCandidate, later_job.active_source_candidate_id)
        assert later_selected is not None
        later_selected.provider_title = "SECOND_JOB_MUST_REMAIN_VISIBLE"
        session.add_all(
            SourceCandidate(
                request_track_id=crowded_job.request_track_id,
                job_id=crowded_job.id,
                provider="youtube",
                extractor="youtube",
                source_id=f"crowded-source-{index}",
                acquisition_url=f"https://www.youtube.com/watch?v=crowded{index:03d}",
                provider_title=f"Crowded candidate {index}",
                provider_artist="Gabry Ponte & KEL",
                uploader="Archive",
                uploader_relationship="third_party",
                duration_seconds=146.0,
                version_signature="studio",
                group_key=f"crowded-group-{index}",
                local_score=0.99,
                policy_status="allowed",
                probe_status="valid",
                contradictions_json="[]",
                sanitized_metadata_json="{}",
            )
            for index in range(60)
        )

    page = client.get("/downloads")
    assert page.status_code == 200
    crowded_article = page.text.split(f'data-job-id="{crowded_job_id}"', 1)[1].split(
        "</article>", 1
    )[0]
    later_article = page.text.split(f'data-job-id="{later_job_id}"', 1)[1].split("</article>", 1)[0]
    assert crowded_article.count("diagnostic-candidate") == 24
    assert "SECOND_JOB_MUST_REMAIN_VISIBLE" in later_article


def test_standard_user_downloads_never_receive_source_resolution_diagnostics(client):
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
    _seed_auto_queued_job(
        client.app.state.session_factory,
        user_id,
        suffix="u",
        status="queued",
        stage="queued",
    )
    login = client.app.state.auth.create_session(user_id)
    client.cookies.set(SESSION_COOKIE, login.token, path="/")

    page = client.get("/downloads")
    assert page.status_code == 200
    assert "Source selected · YouTube" in page.text
    assert "Source resolution diagnostics" not in page.text
    assert "Bounded source searches" not in page.text
    assert "Tarantella &lt;script&gt;" not in page.text
    assert '<img src=x onerror="NEVER_RENDER_UPLOADER_EVENT">' not in page.text
    assert "NEVER_RENDER_QUERY_CREDENTIAL" not in page.text
    assert "NEVER_RENDER_AUTHORIZATION" not in page.text
    fragment = client.get("/downloads?fragment=true")
    assert fragment.status_code == 200
    assert "Source resolution diagnostics" not in fragment.text
    assert "Bounded source searches" not in fragment.text
    assert "NEVER_RENDER_QUERY_CREDENTIAL" not in fragment.text
