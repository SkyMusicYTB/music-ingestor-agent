from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.models import (
    Conversation,
    DownloadJob,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    SourceCandidate,
    User,
)
from app.repositories.decisions import (
    DecisionConflict,
    DecisionSelection,
    apply_review_bundle,
    latest_user_canonical_selection,
    review_bundle_fingerprint,
)
from app.workers.queue import DownloadJobQueue, JobLease


def _active_job(session_factory, suffix: str) -> tuple[str, str, JobLease]:
    with session_factory.begin() as session:
        user = User(
            username=f"decision-{suffix}",
            username_normalized=f"decision-{suffix}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="decision")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Yellow by Coldplay",
            action="add",
            idempotency_key=f"decision-{suffix}",
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
            dedup_key=f"decision:{suffix}",
            status="active",
            stage="resolving_metadata",
            lease_token=f"lease-{suffix}",
            lease_expires_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        )
        session.add(job)
        session.flush()
        return (
            job.id,
            track.id,
            JobLease(
                job_id=job.id,
                token=f"lease-{suffix}",
                approved_snapshot=snapshot,
                retry_count=0,
            ),
        )


def _metadata_options(album: str = "Parachutes") -> list[dict[str, object]]:
    return [
        {
            "kind": "canonical_metadata",
            "rank": 1,
            "recording_candidate_id": "rec_yellow",
            "release_candidate_id": f"rel_{album.casefold()}",
            "artist": "Coldplay",
            "title": "Yellow",
            "album": album,
            "recording_mbid": "cc197bad-dc9c-440d-a5b5-d52ba2e14234",
            "release_mbid": "6f9a5bb4-9273-4e38-897a-19d62b6f7588",
            "score": 0.84,
        }
    ]


def _pending_bundle(session, job_id: str) -> tuple[DownloadJob, JobDecision, JobReviewOption]:
    job = session.get(DownloadJob, job_id)
    assert job is not None
    decision = session.scalar(
        select(JobDecision).where(JobDecision.job_id == job_id, JobDecision.state == "pending")
    )
    assert decision is not None
    option = session.scalar(
        select(JobReviewOption).where(JobReviewOption.decision_id == decision.id)
    )
    assert option is not None
    return job, decision, option


def test_review_bundle_is_atomic_replayable_and_never_presented_twice(
    session_factory,
) -> None:
    job_id, _track_id, lease = _active_job(session_factory, "replay")
    queue = DownloadJobQueue(session_factory)
    options = _metadata_options()
    assert queue.require_review(lease, reason="release conflict", options=options)

    with session_factory.begin() as session:
        job, decision, option = _pending_bundle(session, job_id)
        fingerprint = review_bundle_fingerprint([decision])
        revision = job.decision_revision
        selection = DecisionSelection(decision_id=decision.id, option_id=option.id)
        result = apply_review_bundle(
            session,
            job,
            bundle_fingerprint=fingerprint,
            revision=revision,
            selections=[selection],
        )
        assert not result.replayed
        assert job.status == "queued"
        snapshot = json.loads(job.approved_snapshot_json)
        assert snapshot["album"] == "Parachutes"
        assert "album_constraint_explicit" not in snapshot["metadata_provenance"]
        assert snapshot["metadata_provenance"]["canonical_metadata_resolution"] == {
            "automatic_association": False,
            "source": "user_confirmed_server_candidate",
            "decided_by": "user",
        }

    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        replay = apply_review_bundle(
            session,
            job,
            bundle_fingerprint=fingerprint,
            revision=revision,
            selections=[selection],
        )
        assert replay.replayed

    conflicting_replays = [
        ("0" * 64, revision, [selection]),
        (fingerprint, revision + 1, [selection]),
        (
            fingerprint,
            revision,
            [
                DecisionSelection(
                    decision_id=selection.decision_id,
                    option_id=selection.option_id,
                    correction={"album": "Different release"},
                )
            ],
        ),
    ]
    for replay_fingerprint, replay_revision, replay_selections in conflicting_replays:
        with session_factory.begin() as session:
            job = session.get(DownloadJob, job_id)
            assert job is not None
            with pytest.raises(DecisionConflict, match="already been decided"):
                apply_review_bundle(
                    session,
                    job,
                    bundle_fingerprint=replay_fingerprint,
                    revision=replay_revision,
                    selections=replay_selections,
                )

    with session_factory.begin() as session:
        decision = session.get(JobDecision, selection.decision_id)
        assert decision is not None
        corrupted = json.loads(decision.selected_payload_json or "{}")
        corrupted["_applied_correction"] = {"artist": "Unrecorded correction"}
        decision.selected_payload_json = json.dumps(corrupted)
        session.flush()
        assert latest_user_canonical_selection(session, job_id) is None


def test_multi_decision_review_replay_requires_the_complete_original_bundle(
    session_factory,
) -> None:
    job_id, _track_id, lease = _active_job(session_factory, "multi-replay")
    queue = DownloadJobQueue(session_factory)
    options = [
        *_metadata_options(),
        {
            "kind": "possible_duplicate",
            "rank": 1,
            "track_id": "owned-track",
            "artist": "Coldplay",
            "title": "Yellow",
            "score": 0.80,
        },
    ]
    assert queue.require_review(lease, reason="combined conflict", options=options)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        pending = list(
            session.scalars(
                select(JobDecision).where(
                    JobDecision.job_id == job_id,
                    JobDecision.state == "pending",
                )
            )
        )
        selections = []
        for decision in pending:
            option = session.scalar(
                select(JobReviewOption).where(JobReviewOption.decision_id == decision.id)
            )
            assert option is not None
            selections.append(DecisionSelection(decision.id, option.id))
        correction_index = next(
            index
            for index, decision in enumerate(pending)
            if decision.category == "possible_duplicate"
        )
        selections[correction_index] = DecisionSelection(
            selections[correction_index].decision_id,
            selections[correction_index].option_id,
            correction={"artist": "Corrected Coldplay", "title": "Yellow", "album": None},
        )
        fingerprint = review_bundle_fingerprint(pending)
        revision = job.decision_revision
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=fingerprint,
            revision=revision,
            selections=selections,
        )
        final_snapshot = json.loads(job.approved_snapshot_json)
        assert final_snapshot["artist"] == "Corrected Coldplay"
        assert final_snapshot["album"] is None
        session.flush()
        canonical_replay = latest_user_canonical_selection(session, job_id)
        assert canonical_replay is not None
        assert canonical_replay.payload["artist"] == "Corrected Coldplay"
        assert canonical_replay.payload["album"] is None

    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        with pytest.raises(DecisionConflict, match="already been decided"):
            apply_review_bundle(
                session,
                job,
                bundle_fingerprint=fingerprint,
                revision=revision,
                selections=selections[:1],
            )

    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.status = "active"
        job.stage = "resolving_metadata"
        job.lease_token = "lease-replayed"  # noqa: S105 - inert fencing-token fixture
    replay_lease = JobLease(
        job_id=job_id,
        token="lease-replayed",  # noqa: S106
        approved_snapshot={"artist": "Coldplay", "title": "Yellow"},
        retry_count=0,
    )
    assert not queue.require_review(
        replay_lease,
        reason="same release conflict",
        options=options,
    )
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and job.status == "queued"
        assert (
            session.scalar(select(func.count(JobDecision.id)).where(JobDecision.job_id == job_id))
            == 2
        )


def test_changed_candidate_set_gets_new_revision_without_rank_collision(
    session_factory,
) -> None:
    job_id, _track_id, lease = _active_job(session_factory, "revision")
    queue = DownloadJobQueue(session_factory)
    assert queue.require_review(lease, reason="first conflict", options=_metadata_options())
    with session_factory.begin() as session:
        job, decision, option = _pending_bundle(session, job_id)
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=review_bundle_fingerprint([decision]),
            revision=job.decision_revision,
            selections=[DecisionSelection(decision_id=decision.id, option_id=option.id)],
        )
        job.status = "active"
        job.stage = "resolving_metadata"
        job.lease_token = "lease-revision-two"  # noqa: S105 - inert fencing-token fixture

    second_lease = JobLease(
        job_id=job_id,
        token="lease-revision-two",  # noqa: S106
        approved_snapshot={"artist": "Coldplay", "title": "Yellow"},
        retry_count=0,
    )
    assert queue.require_review(
        second_lease,
        reason="materially changed conflict",
        options=_metadata_options("Yellow (Single)"),
    )
    with session_factory() as session:
        decisions = list(
            session.scalars(
                select(JobDecision)
                .where(JobDecision.job_id == job_id)
                .order_by(JobDecision.revision)
            )
        )
        assert [item.state for item in decisions] == ["selected", "pending"]
        ranks = list(
            session.scalars(
                select(JobReviewOption.rank)
                .where(JobReviewOption.job_id == job_id)
                .order_by(JobReviewOption.revision)
            )
        )
        assert ranks == [1, 1]


def test_review_rejects_stale_bundle_and_source_that_is_no_longer_safe(
    session_factory,
) -> None:
    job_id, track_id, lease = _active_job(session_factory, "unsafe-source")
    with session_factory.begin() as session:
        candidate = SourceCandidate(
            request_track_id=track_id,
            job_id=job_id,
            provider="youtube",
            extractor="youtube",
            source_id="safe-source",
            acquisition_url="https://www.youtube.com/watch?v=safe-source",
            provider_title="Coldplay - Yellow",
            provider_artist="Coldplay",
            uploader="Third Party Archive",
            uploader_relationship="third_party",
            duration_seconds=266.0,
            version_signature="studio",
            group_key="coldplay:yellow:studio",
            local_score=0.83,
            policy_status="allowed",
            probe_status="valid",
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    options = [
        {
            "kind": "acquisition_source",
            "rank": 1,
            "source_candidate_id": candidate_id,
            "provider": "youtube",
            "uploader": "Third Party Archive",
            "score": 0.83,
        }
    ]
    queue = DownloadJobQueue(session_factory)
    assert queue.require_review(lease, reason="source conflict", options=options)
    with session_factory.begin() as session:
        job, decision, option = _pending_bundle(session, job_id)
        fingerprint = review_bundle_fingerprint([decision])
        revision = job.decision_revision
        with pytest.raises(DecisionConflict, match="stale"):
            apply_review_bundle(
                session,
                job,
                bundle_fingerprint="0" * 64,
                revision=revision,
                selections=[DecisionSelection(decision_id=decision.id, option_id=option.id)],
            )
        candidate = session.get(SourceCandidate, candidate_id)
        assert candidate is not None
        candidate.policy_status = "exhausted"
        session.flush()
        with pytest.raises(DecisionConflict, match="no longer safe"):
            apply_review_bundle(
                session,
                job,
                bundle_fingerprint=fingerprint,
                revision=revision,
                selections=[DecisionSelection(decision_id=decision.id, option_id=option.id)],
            )
