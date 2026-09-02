from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.clients.ytdlp import (
    DownloadCancelled,
    DownloadTimedOut,
    SourceValidationError,
    YtDlpError,
)
from app.config import Settings
from app.db.models import (
    Conversation,
    DownloadJob,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    Track,
    User,
)
from app.repositories.decisions import (
    DecisionSelection,
    apply_review_bundle,
    review_bundle_fingerprint,
)
from app.services.duplicates import DuplicateDetector, normalize_text
from app.services.metadata_matching import MetadataCandidate, MetadataMatcher
from app.workers.media import MediaProbe, MediaProcessor
from app.workers.metadata import CanonicalMetadataResolution
from app.workers.processor import (
    DownloadJobProcessor,
    DuplicateOwned,
    JobNeedsReview,
    _is_retryable_job_error,
    _is_transient_source_error,
)
from app.workers.queue import DownloadJobQueue, JobLease


def _processor_for_metadata_check() -> DownloadJobProcessor:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.metadata_matcher = MetadataMatcher()
    return processor


def test_canonical_metadata_requires_auto_match() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=180,
        bitrate=160_000,
    )

    with pytest.raises(JobNeedsReview, match="does not confidently match"):
        processor._validate_canonical_metadata(
            {"artist": "Expected Artist", "title": "Expected Song"},
            {"artist": "Completely Different", "title": "Unrelated Upload"},
            probe,
        )


def test_canonical_metadata_allows_auto_match() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=180,
        bitrate=160_000,
    )

    processor._validate_canonical_metadata(
        {
            "artist": "Expected Artist",
            "title": "Expected Song",
            "duration_seconds": 180,
        },
        {"artist": "Expected Artist - Topic", "track": "Expected Song"},
        probe,
    )


def test_post_download_match_uses_title_pair_not_third_party_creator() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=266,
        bitrate=160_000,
    )

    processor._validate_canonical_metadata(
        {"artist": "Coldplay", "title": "Yellow", "duration_seconds": 266},
        {
            "title": "Coldplay - Yellow",
            "creator": "Unrelated Fan Archive",
            "uploader": "Unrelated Fan Archive",
        },
        probe,
    )


def test_post_download_match_rejects_cover_even_when_uploader_masks_performer() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=266,
        bitrate=160_000,
    )

    with pytest.raises(JobNeedsReview, match="contradictory recording version"):
        processor._validate_canonical_metadata(
            {"artist": "Coldplay", "title": "Yellow", "duration_seconds": 266},
            {
                "track": "Yellow",
                "title": "Coldplay - Yellow (Cover)",
                "creator": "Cover Performer",
                "uploader": "Cover Performer",
            },
            probe,
        )


def test_download_validation_rejects_an_explicit_version_mismatch() -> None:
    processor = _processor_for_metadata_check()
    probe = MediaProbe(
        path=Path("download.opus"),
        codec="opus",
        container=("ogg",),
        duration_seconds=180,
        bitrate=160_000,
    )

    with pytest.raises(JobNeedsReview, match="does not confidently match"):
        processor._validate_canonical_metadata(
            {
                "artist": "Expected Artist",
                "title": "Expected Song",
                "requested_version": "live",
                "metadata_provenance": {
                    "request_constraints": {"version_constraint_explicit": True}
                },
            },
            {"artist": "Expected Artist", "track": "Expected Song"},
            probe,
        )


class _CanonicalResolver:
    def __init__(self, resolution: CanonicalMetadataResolution) -> None:
        self.resolution = resolution
        self.calls: list[dict[str, object]] = []

    def resolve(self, **kwargs: object) -> CanonicalMetadataResolution:
        self.calls.append(kwargs)
        return self.resolution


def test_musicbrainz_auto_match_enriches_canonical_tags() -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    candidate = MetadataCandidate(
        artist="Canonical Artist",
        title="Canonical Song",
        album="Canonical Album",
        year=1999,
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
        release_group_mbid="33333333-3333-3333-3333-333333333333",
    )
    resolver = _CanonicalResolver(CanonicalMetadataResolution("auto", candidate, (), "exact"))
    processor.metadata_resolver = resolver  # type: ignore[assignment]
    probe = MediaProbe(Path("download.opus"), "opus", ("ogg",), 180, 160_000)

    values = processor._resolve_canonical_metadata(
        {
            "artist": "Uploader",
            "title": "Upload Title",
            "album": "Model-inferred Album",
            "version_signature": "live",
            "metadata_provenance": {
                "request_constraints": {
                    "album_constraint_explicit": False,
                    "version_constraint_explicit": False,
                }
            },
        },
        probe,
    )

    assert values["artist"] == "Canonical Artist"
    assert values["title"] == "Canonical Song"
    assert values["album"] == "Canonical Album"
    assert values["recording_mbid"] == candidate.recording_mbid
    assert resolver.calls[0]["album"] is None
    assert resolver.calls[0]["version_signature"] is None
    assert resolver.calls[0]["album_is_explicit"] is False


def test_only_explicit_album_provenance_reaches_release_precedence() -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    candidate = MetadataCandidate(artist="Artist", title="Song", album="Requested Edition")
    resolver = _CanonicalResolver(CanonicalMetadataResolution("auto", candidate, (), "exact"))
    processor.metadata_resolver = resolver  # type: ignore[assignment]
    probe = MediaProbe(Path("download.opus"), "opus", ("ogg",), 180, 160_000)

    processor._resolve_canonical_metadata(
        {
            "artist": "Artist",
            "title": "Song",
            "album": "Model-inferred Album",
            "version_signature": "studio",
            "requested_album": "Requested Edition",
            "requested_version": "live",
            "metadata_provenance": {
                "request_constraints": {
                    "album_constraint_explicit": True,
                    "version_constraint_explicit": True,
                }
            },
        },
        probe,
    )

    assert resolver.calls[0]["album"] == "Requested Edition"
    assert resolver.calls[0]["version_signature"] == "live"
    assert resolver.calls[0]["album_is_explicit"] is True


def test_canonical_review_preserves_explicit_constraints_when_resumed(
    session_factory,
) -> None:
    original_constraints = {
        "requested_album": "Live 2012",
        "album_constraint_explicit": True,
        "requested_version": "live",
        "version_constraint_explicit": True,
    }
    original_provenance = {
        "source": "user_request",
        "album_constraint_explicit": True,
        "version_constraint_explicit": True,
        "request_constraints": original_constraints,
        "unrelated_provenance": "preserve-me",
    }
    snapshot: dict[str, object] = {
        "artist": "Coldplay",
        "title": "Yellow",
        "album": "Live 2012",
        "version_signature": "live",
        "requested_album": "Live 2012",
        "requested_version": "live",
        "album_constraint_explicit": True,
        "version_constraint_explicit": True,
        "metadata_provenance": original_provenance,
    }
    with session_factory.begin() as session:
        user = User(
            username="review-live",
            username_normalized="review-live",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="review live")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add the live version of Yellow by Coldplay from album Live 2012",
            action="add",
            idempotency_key="review-live",
        )
        session.add(request)
        session.flush()
        request_track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Coldplay",
            title="Yellow",
            album="Live 2012",
            version_signature="live",
            selected=True,
        )
        session.add(request_track)
        session.flush()
        snapshot["request_track_id"] = request_track.id
        job = DownloadJob(
            request_track_id=request_track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key="canonical-review-live",
            status="active",
            stage="resolving_metadata",
            lease_token="lease-review-live",  # noqa: S106
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    candidate = MetadataCandidate(
        artist="Coldplay",
        title="Yellow (Live)",
        album="Live 2012",
        duration_seconds=270.0,
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
    )
    option = {
        "kind": "canonical_metadata",
        "rank": 1,
        "recording_candidate_id": "rec_yellow_live",
        "release_candidate_id": "rel_live_2012",
        "artist": candidate.artist,
        "title": candidate.title,
        "album": candidate.album,
        "duration_seconds": candidate.duration_seconds,
        "recording_mbid": candidate.recording_mbid,
        "release_mbid": candidate.release_mbid,
        "version": "live",
        "score": 0.82,
    }
    queue = DownloadJobQueue(session_factory)
    initial_lease = JobLease(
        job_id=job_id,
        token="lease-review-live",  # noqa: S106
        approved_snapshot=snapshot,
        retry_count=0,
    )
    assert queue.require_review(
        initial_lease,
        reason="two plausible live releases",
        options=[option],
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "canonical_metadata",
                JobDecision.state == "pending",
            )
        )
        assert decision is not None
        review_option = session.scalar(
            select(JobReviewOption).where(JobReviewOption.decision_id == decision.id)
        )
        assert review_option is not None
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=review_bundle_fingerprint([decision]),
            revision=job.decision_revision,
            selections=[
                DecisionSelection(
                    decision.id,
                    review_option.id,
                    correction={
                        "artist": "Coldplay & Guest",
                        "title": "Yellow (Live in Madrid)",
                        "album": "Live in Madrid",
                    },
                )
            ],
        )
        reviewed_snapshot = json.loads(job.approved_snapshot_json)
        assert reviewed_snapshot["artist"] == "Coldplay & Guest"
        assert reviewed_snapshot["title"] == "Yellow (Live in Madrid)"
        assert reviewed_snapshot["album"] == "Live in Madrid"
        assert reviewed_snapshot["requested_version"] == "live"
        assert reviewed_snapshot["requested_album"] == "Live 2012"
        assert reviewed_snapshot["album_constraint_explicit"] is True
        assert reviewed_snapshot["version_constraint_explicit"] is True
        provenance = reviewed_snapshot["metadata_provenance"]
        assert provenance["request_constraints"] == original_constraints
        assert provenance["album_constraint_explicit"] is True
        assert provenance["version_constraint_explicit"] is True
        assert provenance["unrelated_provenance"] == "preserve-me"
        assert provenance["user_constraints"] == {
            "album_constraint_explicit": True,
            "requested_album": "Live in Madrid",
        }
        assert provenance["canonical_metadata_resolution"]["decided_by"] == "user"
        selected_payload = json.loads(decision.selected_payload_json or "{}")
        assert selected_payload["artist"] == "Coldplay & Guest"
        assert selected_payload["title"] == "Yellow (Live in Madrid)"
        assert selected_payload["album"] == "Live in Madrid"
        assert selected_payload["_applied_correction"] == {
            "album": "Live in Madrid",
            "artist": "Coldplay & Guest",
            "title": "Yellow (Live in Madrid)",
        }

    resumed_lease = queue.claim_next()
    assert resumed_lease is not None and resumed_lease.job_id == job_id
    resolver = _CanonicalResolver(
        CanonicalMetadataResolution("review", candidate, (option,), "ambiguous")
    )
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.metadata_resolver = resolver  # type: ignore[assignment]
    processor.session_factory = session_factory
    resolved = processor._resolve_canonical_metadata(
        resumed_lease.approved_snapshot,
        MediaProbe(Path("download.opus"), "opus", ("ogg",), 270.0, 160_000),
        lease=resumed_lease,
    )

    assert resolver.calls == []
    assert resolved["artist"] == "Coldplay & Guest"
    assert resolved["title"] == "Yellow (Live in Madrid)"
    assert resolved["album"] == "Live in Madrid"
    assert resolved["requested_version"] == "live"
    assert resolved["requested_album"] == "Live 2012"
    assert resolved["album_constraint_explicit"] is True
    assert resolved["version_constraint_explicit"] is True
    assert resolved["metadata_provenance"]["request_constraints"] == original_constraints
    assert resolved["metadata_provenance"]["canonical_metadata_resolution"] == {
        "automatic_association": False,
        "source": "user_confirmed_server_candidate",
        "decided_by": "user",
    }
    with session_factory() as session:
        decisions = list(
            session.scalars(
                select(JobDecision).where(
                    JobDecision.job_id == job_id,
                    JobDecision.category == "canonical_metadata",
                )
            )
        )
        assert len(decisions) == 1
        assert decisions[0].state == "selected"


def test_openai_cannot_override_a_local_explicit_album_contradiction(settings) -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.settings = settings
    recording_id = "rec_11111111111111111111"
    release_id = "rel_22222222222222222222"
    option = {
        "recording_candidate_id": recording_id,
        "release_candidate_id": release_id,
        "artist": "Coldplay",
        "title": "Yellow",
        "album": "Xylophone Dreams",
        "version": "studio",
        "duration_seconds": 266,
        "local_score": 0.95,
        # Even if a future option serializer drops its local contradiction code,
        # the backend must re-check the authoritative explicit album itself.
        "contradiction_codes": [],
    }
    values = {
        "artist": "Coldplay",
        "title": "Yellow",
        "requested_album": "Parachutes",
        "metadata_provenance": {"request_constraints": {"album_constraint_explicit": True}},
    }
    model_result = {
        "decision": {
            "selected_recording_candidate_id": recording_id,
            "selected_release_candidate_id": release_id,
            "recording_version": "studio",
            "decision": "match",
            "confidence": 0.99,
            "contradiction_codes": [],
            "reason_code": "model_match",
        }
    }

    selected, _decision = processor._adjudicate_canonical_model(
        values,
        MediaProbe(Path("download.opus"), "opus", ("ogg",), 266, 160_000),
        [option],
        model_result,
    )

    assert selected is None


@pytest.mark.parametrize(
    ("selected_rank", "uses_existing"),
    [(1, True), (2, False)],
)
def test_possible_duplicate_decision_is_fingerprint_scoped_and_actionable(
    session_factory,
    tmp_path: Path,
    selected_rank: int,
    uses_existing: bool,
) -> None:
    music_root = tmp_path / "music"
    music_root.mkdir(exist_ok=True)
    existing_path = music_root / "Coldplay - Yellow.opus"
    existing_path.write_bytes(b"existing-audio")
    with session_factory.begin() as session:
        user = User(
            username=f"duplicate-{selected_rank}",
            username_normalized=f"duplicate-{selected_rank}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="duplicate")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Yelow by Coldplay",
            action="add",
            idempotency_key=f"duplicate-{selected_rank}",
        )
        session.add(request)
        session.flush()
        request_track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Coldplay",
            title="Yelow",
            selected=True,
        )
        session.add(request_track)
        session.flush()
        snapshot = {
            "request_track_id": request_track.id,
            "artist": "Coldplay",
            "title": "Yelow",
            "version_signature": "studio",
        }
        job = DownloadJob(
            request_track_id=request_track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key=f"possible-duplicate-{selected_rank}",
            status="active",
            stage="verifying",
            lease_token=f"lease-{selected_rank}",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.add(
            Track(
                artist="Coldplay",
                artist_normalized=normalize_text("Coldplay"),
                title="Yellow",
                title_normalized=normalize_text("Yellow"),
                album="Parachutes",
                version_signature="studio",
                duration_seconds=269.0,
                filepath=existing_path.name,
                file_mtime_ns=existing_path.stat().st_mtime_ns,
                file_size=existing_path.stat().st_size,
                file_sha256=None,
                scan_generation=1,
            )
        )
        session.flush()
        job_id = job.id
    lease = JobLease(
        job_id=job_id,
        token=f"lease-{selected_rank}",
        approved_snapshot=snapshot,
        retry_count=0,
    )
    runtime_settings = Settings(
        environment="test",
        music_path=music_root,
        database_path=tmp_path / "test.db",
    )
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.session_factory = session_factory
    processor.settings = runtime_settings
    processor.duplicate_detector = DuplicateDetector(music_root)
    probe = MediaProbe(Path("candidate.opus"), "opus", ("ogg",), 269.0, 160_000)
    values = {"artist": "Coldplay", "title": "Yelow", "version_signature": "studio"}

    with pytest.raises(JobNeedsReview) as raised:
        processor._check_duplicate(job_id, values, probe)
    assert [option["duplicate_action"] for option in raised.value.options] == [
        "use_existing",
        "import_separate",
    ]
    queue = DownloadJobQueue(session_factory)
    assert queue.require_review(lease, reason=raised.value.reason, options=raised.value.options)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "possible_duplicate",
                JobDecision.state == "pending",
            )
        )
        assert decision is not None
        option = session.scalar(
            select(JobReviewOption).where(
                JobReviewOption.decision_id == decision.id,
                JobReviewOption.rank == selected_rank,
            )
        )
        assert option is not None
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=review_bundle_fingerprint([decision]),
            revision=job.decision_revision,
            selections=[DecisionSelection(decision.id, option.id)],
        )

    if uses_existing:
        with pytest.raises(DuplicateOwned):
            processor._check_duplicate(job_id, values, probe)
    else:
        processor._check_duplicate(job_id, values, probe)


def test_musicbrainz_review_match_stops_before_tagging() -> None:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    candidate = MetadataCandidate(artist="Possible Artist", title="Possible Song")
    option = {
        "kind": "metadata",
        "rank": 1,
        "artist": candidate.artist,
        "title": candidate.title,
        "score": 0.82,
    }
    processor.metadata_resolver = _CanonicalResolver(  # type: ignore[assignment]
        CanonicalMetadataResolution("review", candidate, (option,), "ambiguous")
    )
    probe = MediaProbe(Path("download.opus"), "opus", ("ogg",), 180, 160_000)

    with pytest.raises(JobNeedsReview) as raised:
        processor._resolve_canonical_metadata(
            {"artist": "Artist", "title": "Song", "version_signature": "studio"},
            probe,
        )

    assert raised.value.options == [option]


def test_ffprobe_inspection_honors_cancellation_signal(tmp_path: Path) -> None:
    executable = tmp_path / "fake-media-tool"
    executable.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
    executable.chmod(0o755)
    media = tmp_path / "audio.opus"
    media.write_bytes(b"synthetic")
    processor = MediaProcessor(ffprobe=str(executable), ffmpeg=str(executable))
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(DownloadCancelled):
        processor.inspect(media, max_duration_seconds=1800, cancel_signal=cancelled)


@pytest.mark.parametrize(
    "error",
    [
        DownloadTimedOut("download timed out"),
        YtDlpError("HTTP Error 503: Service Unavailable"),
        SourceValidationError("source host could not be resolved"),
    ],
)
def test_transient_source_failures_retry_without_consuming_candidate(error: Exception) -> None:
    assert _is_transient_source_error(error)
    assert _is_retryable_job_error(error)


@pytest.mark.parametrize(
    "error",
    [
        YtDlpError("video unavailable"),
        SourceValidationError("source address resolved to a non-global network"),
    ],
)
def test_source_specific_failures_are_not_job_retries(error: Exception) -> None:
    assert not _is_transient_source_error(error)
    assert not _is_retryable_job_error(error)
