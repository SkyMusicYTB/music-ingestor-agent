from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.cli import build_parser, main
from app.db.models import (
    Conversation,
    DownloadJob,
    Event,
    JobArtifact,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    SourceCandidate,
    User,
)
from app.repositories.decisions import (
    DecisionSelection,
    apply_review_bundle,
    candidate_set_fingerprint,
    create_pending_decision,
    latest_canonical_selection,
    record_selected_decision,
    review_bundle_fingerprint,
)
from app.repositories.jobs import JobRepository
from app.services.metadata_review_repair import (
    LEGACY_METADATA_ERROR,
    repair_empty_metadata_reviews,
)
from app.workers.queue import DownloadJobQueue, JobLease


def seed_job(factory, *, legacy=False):
    with factory.begin() as session:
        user = User(
            username="owner",
            username_normalized="owner",
            password_hash="fixture",  # noqa: S106 - inert fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Tarantella")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="fixture direct URL",
            action="add",
            idempotency_key="fixture",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id, ordinal=1, artist="Gabry Ponte, KEL", title="Tarantella"
        )
        session.add(track)
        session.flush()
        snapshot = {
            "artist": track.artist,
            "title": track.title,
            "recording_mbid": "stale-suggested-identifier",
            "canonical_identity_verified": True,
        }
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key="fixture",
            status="needs_review" if legacy else "active",
            stage="resolving_source" if legacy else "resolving_metadata",
            lease_token=None if legacy else "fixture-lease",
            error_message=LEGACY_METADATA_ERROR if legacy else None,
            decision_revision=1 if legacy else 0,
        )
        session.add(job)
        session.flush()
        source = SourceCandidate(
            request_track_id=track.id,
            job_id=job.id,
            provider="youtube",
            extractor="youtube",
            source_id="rxw1RCAY3qw",
            acquisition_url="https://www.youtube.com/watch?v=rxw1RCAY3qw",
            provider_title="Tarantella",
            provider_artist="Gabry Ponte, KEL",
            uploader="Gabry Ponte",
            duration_seconds=146,
            group_key="fixture",
            local_score=1.0,
            policy_status="allowed",
            probe_status="valid",
        )
        session.add(source)
        session.flush()
        job.active_source_candidate_id = source.id
        if legacy:
            session.add(
                JobDecision(
                    job_id=job.id,
                    category="acquisition_source",
                    state="pending",
                    revision=1,
                    candidate_set_fingerprint=candidate_set_fingerprint("acquisition_source", []),
                    reason_codes_json=json.dumps([LEGACY_METADATA_ERROR]),
                )
            )
        return job.id, user.id, source.id, JobLease(job.id, "fixture-lease", snapshot, 0)


def provider_option():
    return {
        "kind": "canonical_metadata",
        "artist": "Gabry Ponte, KEL",
        "title": "Tarantella",
        "year": 2024,
        "album": None,
        "duration_seconds": 146,
        "artists": ["Gabry Ponte", "KEL"],
        "metadata_authority": "direct_user_source",
        "canonical_identity_verified": False,
        "recording_mbid": None,
        "release_mbid": None,
        "release_group_mbid": None,
        "source_provider": "youtube",
        "source_extractor": "youtube",
        "source_id": "rxw1RCAY3qw",
        "source_url": "https://www.youtube.com/watch?v=rxw1RCAY3qw",
        "source_uploader": "Gabry Ponte",
        "version": "studio",
    }


def test_provider_review_is_durable_null_mbid_and_source_bound(session_factory):
    job_id, _, source_id, lease = seed_job(session_factory)
    queue = DownloadJobQueue(session_factory)
    reason = "No confident MusicBrainz match was found. Use source metadata or correct it."
    queue.add_warning(lease, code="thumbnail", message="Source artwork is a fallback")
    assert queue.require_review(
        lease, reason=reason, options=[provider_option()], category="canonical_metadata"
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job.stage == "resolving_metadata"
        assert job.active_source_candidate_id == source_id
        assert json.loads(job.warnings_json) == [
            {"code": "thumbnail", "message": "Source artwork is a fallback"}
        ]
        decision = session.scalar(select(JobDecision).where(JobDecision.job_id == job_id))
        option = session.scalar(select(JobReviewOption).where(JobReviewOption.job_id == job_id))
        selected = DecisionSelection(
            decision.id, option.id, {"artist": "Gabry Ponte & KEL", "year": 2024}
        )
        fingerprint = review_bundle_fingerprint([decision])
        revision = job.decision_revision
        assert not apply_review_bundle(
            session, job, bundle_fingerprint=fingerprint, revision=revision, selections=[selected]
        ).replayed
        snapshot = json.loads(job.approved_snapshot_json)
        assert snapshot["canonical_identity_verified"] is False
        assert snapshot["recording_mbid"] is None
        assert snapshot["metadata_authority"] == "user_confirmed_provider_metadata"
        assert snapshot["year"] == 2024
        assert job.active_source_candidate_id == source_id
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert apply_review_bundle(
            session, job, bundle_fingerprint=fingerprint, revision=revision, selections=[selected]
        ).replayed
        replay = latest_canonical_selection(
            session, job_id, source_extractor="youtube", source_id="rxw1RCAY3qw"
        )
        assert replay is not None
        assert replay.payload["metadata_authority"] == "user_confirmed_provider_metadata"
        assert replay.payload["canonical_identity_verified"] is False
        assert replay.payload["recording_mbid"] is None
        assert replay.payload["artist"] == "Gabry Ponte & KEL"
        assert replay.payload["artists"] == ["Gabry Ponte & KEL"]
        assert latest_canonical_selection(session, job_id, source_id="different") is None


def test_empty_reviews_fail_without_pending_fingerprints(session_factory):
    job_id, _, _, lease = seed_job(session_factory)
    queue = DownloadJobQueue(session_factory)
    assert not queue.require_review(
        lease, reason="No metadata", options=[], category="canonical_metadata"
    )
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job.status == "failed"
        assert job.stage == "resolving_metadata"
        assert job.error_code == "review_has_no_options"
        assert session.scalar(select(func.count()).select_from(JobDecision)) == 0
        with pytest.raises(ValueError, match="actionable"):
            create_pending_decision(
                session, job, category="canonical_metadata", reason="x", options=[]
            )


def test_automatic_metadata_replay_preserves_authority_reason_and_source(session_factory):
    job_id, _, _, _ = seed_job(session_factory)
    option = {**provider_option(), "reason_code": "musicbrainz_unavailable"}
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        record_selected_decision(
            session,
            job,
            category="canonical_metadata",
            candidates=[option],
            selected_payload=option,
            decided_by="deterministic",
            reason_codes=["provider_fallback"],
        )
    with session_factory() as session:
        replay = latest_canonical_selection(session, job_id, source_id="rxw1RCAY3qw")
        assert replay is not None
        assert replay.decided_by == "deterministic"
        assert replay.payload["reason_code"] == "musicbrainz_unavailable"
        assert replay.payload["metadata_authority"] == "direct_user_source"
        assert replay.payload["recording_mbid"] is None
        assert latest_canonical_selection(session, job_id, source_extractor="soundcloud") is None


def test_legacy_repair_dry_run_apply_and_replay_preserve_source(session_factory):
    job_id, _, source_id, _ = seed_job(session_factory, legacy=True)
    assert repair_empty_metadata_reviews(session_factory) == {
        "mode": "dry-run",
        "count": 1,
        "job_ids": [job_id],
    }
    with session_factory() as session:
        assert session.get(DownloadJob, job_id).status == "needs_review"
        assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert (
        repair_empty_metadata_reviews(session_factory, apply=True, source_id="not-it")["count"] == 0
    )
    assert (
        repair_empty_metadata_reviews(session_factory, apply=True, source_id="rxw1RCAY3qw")["count"]
        == 1
    )
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job.status == "queued"
        assert job.active_source_candidate_id == source_id
        assert job.stage == "resolving_metadata"
        assert job.error_message is None
        assert session.scalar(select(JobDecision.state)) == "superseded"
    assert repair_empty_metadata_reviews(session_factory, apply=True)["count"] == 0


@pytest.mark.parametrize(
    "difference",
    [
        "active",
        "other_error",
        "option",
        "unsafe",
        "contradiction",
        "canonical_review",
        "cancelled",
        "wrong_url",
    ],
)
def test_repair_does_not_touch_unrelated_or_unsafe_jobs(session_factory, difference):
    job_id, _, source_id, _ = seed_job(session_factory, legacy=True)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        source = session.get(SourceCandidate, source_id)
        decision = session.scalar(select(JobDecision))
        if difference == "active":
            job.status = "active"
        elif difference == "other_error":
            job.error_message = "No permitted source"
        elif difference == "unsafe":
            source.policy_status = "rejected"
        elif difference == "contradiction":
            source.contradictions_json = '["cover"]'
        elif difference == "canonical_review":
            decision.category = "canonical_metadata"
        elif difference == "cancelled":
            job.cancel_requested_at = datetime.now(UTC)
        elif difference == "wrong_url":
            source.acquisition_url = "https://127.0.0.1/secret"
        elif difference == "option":
            session.add(
                JobReviewOption(
                    job_id=job.id,
                    decision_id=decision.id,
                    kind="acquisition_source",
                    rank=1,
                    provider_payload_json="{}",
                    score=1,
                )
            )
    assert repair_empty_metadata_reviews(session_factory, apply=True)["count"] == 0
    with session_factory() as session:
        assert session.get(JobDecision, decision.id).state == "pending"


def test_owned_retry_repairs_only_the_legacy_shape(session_factory):
    job_id, owner, source_id, _ = seed_job(session_factory, legacy=True)
    repository = JobRepository(session_factory)
    with pytest.raises(LookupError):
        repository.mutate_for_user(job_id, "different-owner", "retry")
    job = repository.mutate_for_user(job_id, owner, "retry")
    assert job.status == "queued"
    assert job.stage == "resolving_metadata"
    assert job.active_source_candidate_id == source_id
    with session_factory() as session:
        assert session.scalar(select(JobDecision.state)) == "superseded"


def test_repair_cli_defaults_read_only_and_scopes_source(
    settings, session_factory, monkeypatch, capsys
):
    job_id, _, _, _ = seed_job(session_factory, legacy=True)
    parser = build_parser()
    assert parser.parse_args(["repair-empty-metadata-reviews"]).apply is False
    monkeypatch.setattr("app.cli.Settings", lambda: settings)
    before = settings.database_path.read_bytes()
    main(["repair-empty-metadata-reviews", "--source-id", "rxw1RCAY3qw"])
    assert json.loads(capsys.readouterr().out)["job_ids"] == [job_id]
    assert settings.database_path.read_bytes() == before


def test_completed_staging_is_preserved_for_only_bounded_metadata_wait(session_factory):
    job_id, _, _, _ = seed_job(session_factory, legacy=True)
    queue = DownloadJobQueue(session_factory)
    assert job_id not in queue.staging_job_ids_to_preserve()
    with session_factory.begin() as session:
        artifact = JobArtifact(
            job_id=job_id,
            kind="completed_media",
            stage="resolving_metadata",
            relative_path="media.m4a",
            status="ready",
            updated_at=datetime.now(UTC),
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id
    assert job_id in queue.staging_job_ids_to_preserve()
    with session_factory.begin() as session:
        session.get(JobArtifact, artifact_id).updated_at = datetime.now(UTC) - timedelta(days=8)
    assert job_id not in queue.staging_job_ids_to_preserve()
