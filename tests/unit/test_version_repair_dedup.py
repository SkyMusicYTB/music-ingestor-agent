from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Conversation, DownloadJob, Request, RequestTrack, User
from app.repositories.jobs import dedup_key
from app.services.duplicates import normalize_version_signature, versions_compatible
from app.sources import SourceIntent
from app.workers.processor import DownloadJobProcessor
from app.workers.queue import DownloadJobQueue, JobLease
from app.workers.source_resolution import EquivalentAcquisitionActive, WorkerSourceResolver


class _NoNetworkProvider:
    pass


def _request_track(
    session: Session,
    *,
    user: User,
    suffix: str,
    version: str,
) -> RequestTrack:
    conversation = Conversation(user_id=user.id, title=f"fixture-{suffix}")
    session.add(conversation)
    session.flush()
    request = Request(
        user_id=user.id,
        conversation_id=conversation.id,
        raw_text="add Tarantella by Gabry Ponte and KEL",
        action="add",
        idempotency_key=f"repair-{suffix}",
    )
    session.add(request)
    session.flush()
    track = RequestTrack(
        request_id=request.id,
        ordinal=1,
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album="Battiti Live Compilation 2024",
        duration_seconds=146.0,
        version_signature=version,
        metadata_provenance_json=json.dumps(
            {
                "request_constraints": {"version_constraint_explicit": False},
                "recording_version": {
                    "signature": version,
                    "source": "release_metadata",
                },
            },
            separators=(",", ":"),
        ),
    )
    session.add(track)
    session.flush()
    return track


def _legacy_job(
    factory: sessionmaker[Session],
    *,
    include_studio_conflict: bool,
) -> tuple[str, str, str, dict[str, object]]:
    now = datetime.now(UTC)
    with factory.begin() as session:
        user = User(
            username="repair-owner",
            username_normalized="repair-owner",
            password_hash="fixture",  # noqa: S106 - inert database fixture
        )
        session.add(user)
        session.flush()
        legacy_track = _request_track(session, user=user, suffix="legacy", version="live")
        snapshot: dict[str, object] = {
            "request_track_id": legacy_track.id,
            "artist": legacy_track.artist,
            "artists": ["Gabry Ponte", "KEL"],
            "title": legacy_track.title,
            "album": legacy_track.album,
            "duration_seconds": legacy_track.duration_seconds,
            "version_signature": "live",
            "metadata_provenance": {
                "request_constraints": {"version_constraint_explicit": False},
                "recording_version": {
                    "signature": "live",
                    "source": "release_metadata",
                },
            },
        }
        legacy = DownloadJob(
            request_track_id=legacy_track.id,
            approved_snapshot_json=json.dumps(snapshot, separators=(",", ":")),
            dedup_key=dedup_key(legacy_track),
            status="active",
            stage="resolving_source",
            lease_token="repair-lease",  # noqa: S106 - fencing-token fixture
            lease_expires_at=now + timedelta(minutes=2),
        )
        session.add(legacy)
        session.flush()
        if include_studio_conflict:
            studio_track = _request_track(session, user=user, suffix="studio", version="studio")
            session.add(
                DownloadJob(
                    request_track_id=studio_track.id,
                    approved_snapshot_json="{}",
                    dedup_key=dedup_key(studio_track),
                    status="queued",
                    stage="queued",
                )
            )
        return legacy.id, legacy_track.id, legacy.dedup_key, snapshot


def _resolver(settings: Settings, factory: sessionmaker[Session]) -> WorkerSourceResolver:
    return WorkerSourceResolver(
        settings,
        factory,
        DownloadJobQueue(factory),
        _NoNetworkProvider(),  # type: ignore[arg-type]
    )


def _intent() -> SourceIntent:
    return SourceIntent(
        artist="Gabry Ponte & KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        requested_version="studio",
        duration_seconds=146.0,
    )


def test_version_aliases_share_one_durable_signature_and_dedup_identity() -> None:
    left = RequestTrack(
        request_id="request-left",
        ordinal=1,
        artist="Artist",
        title="Song",
        version_signature="radio_edit+sped_up",
    )
    right = RequestTrack(
        request_id="request-right",
        ordinal=1,
        artist="Artist",
        title="Song",
        version_signature="sped up|radio edit",
    )

    assert normalize_version_signature(left.version_signature) == "radio edit|sped up"
    assert versions_compatible(left.version_signature, right.version_signature)
    assert dedup_key(left) == dedup_key(right)


@pytest.mark.parametrize(
    ("left_version", "right_version", "canonical"),
    [
        ("extended", "extended mix", "extended"),
        ("nightcore", "Nightcore", "nightcore"),
    ],
)
def test_extended_and_nightcore_share_canonical_dedup_identity(
    left_version: str, right_version: str, canonical: str
) -> None:
    left = RequestTrack(
        request_id="request-left",
        ordinal=1,
        artist="Artist",
        title="Song",
        version_signature=left_version,
    )
    right = RequestTrack(
        request_id="request-right",
        ordinal=1,
        artist="Artist",
        title="Song",
        version_signature=right_version,
    )

    assert normalize_version_signature(left.version_signature) == canonical
    assert normalize_version_signature(right.version_signature) == canonical
    assert versions_compatible(left.version_signature, right.version_signature)
    assert dedup_key(left) == dedup_key(right)


def test_compound_featured_version_signature_is_idempotent() -> None:
    raw = "feat. Guest Artist|extended+live"
    canonical = "extended|feat guest artist|live"

    assert normalize_version_signature(raw) == canonical
    assert normalize_version_signature(canonical) == canonical
    assert versions_compatible(raw, canonical)


def test_raw_feature_credit_preserves_plus_sign_participants() -> None:
    assert normalize_version_signature("feat. Alice+Bob") == "feat alice bob"


def test_legacy_version_repair_atomically_rekeys_active_job(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    job_id, track_id, old_key, snapshot = _legacy_job(
        session_factory, include_studio_conflict=False
    )
    lease = JobLease(job_id, "repair-lease", snapshot, 0)

    _resolver(settings, session_factory)._repair_inferred_version_snapshot(lease, _intent())

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        assert track.version_signature == "studio"
        assert job.dedup_key == dedup_key(track)
        assert job.dedup_key != old_key
        assert json.loads(job.approved_snapshot_json)["version_signature"] == "studio"
    assert lease.approved_snapshot["version_signature"] == "studio"


def test_conflicting_corrected_identity_defers_without_partial_repair(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    job_id, track_id, old_key, snapshot = _legacy_job(session_factory, include_studio_conflict=True)
    lease = JobLease(job_id, "repair-lease", snapshot, 0)
    resolver = _resolver(settings, session_factory)

    with pytest.raises(EquivalentAcquisitionActive):
        resolver._repair_inferred_version_snapshot(lease, _intent())

    queue = DownloadJobQueue(session_factory)
    assert queue.defer_for_equivalent_acquisition(lease) == "retry_wait"
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        assert track.version_signature == "live"
        assert job.dedup_key == old_key
        assert json.loads(job.approved_snapshot_json)["version_signature"] == "live"
        assert job.retry_count == 0
        assert job.error_code == "equivalent_acquisition_active"
        assert job.lease_token is None
    assert lease.approved_snapshot["version_signature"] == "live"


def test_genuine_recording_disambiguation_is_never_repaired_from_plain_title(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    job_id, track_id, old_key, snapshot = _legacy_job(
        session_factory, include_studio_conflict=False
    )
    provenance = {
        "automatic_association": True,
        "source": "musicbrainz_search_recordings",
        "artists": ["Gabry Ponte", "KEL"],
        "request_constraints": {"version_constraint_explicit": False},
        "recording_version": {
            "signature": "live",
            "source": "musicbrainz_recording_disambiguation",
        },
    }
    snapshot["canonical_identity_verified"] = True
    snapshot["metadata_provenance"] = provenance
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        job.approved_snapshot_json = json.dumps(snapshot, separators=(",", ":"))
        track.metadata_provenance_json = json.dumps(provenance, separators=(",", ":"))
    lease = JobLease(job_id, "repair-lease", snapshot, 0)

    _resolver(settings, session_factory)._repair_inferred_version_snapshot(lease, _intent())

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        assert track.version_signature == "live"
        assert job.dedup_key == old_key
        assert json.loads(job.approved_snapshot_json)["version_signature"] == "live"


def test_later_identity_correction_obeys_active_dedup_uniqueness_without_partial_write(
    session_factory: sessionmaker[Session],
) -> None:
    job_id, track_id, old_key, snapshot = _legacy_job(session_factory, include_studio_conflict=True)
    lease = JobLease(job_id, "repair-lease", snapshot, 0)
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.session_factory = session_factory
    recording_mbid = "11111111-1111-4111-8111-111111111111"
    with session_factory.begin() as session:
        other_track = session.scalar(
            select(RequestTrack).where(RequestTrack.id != track_id).order_by(RequestTrack.id)
        )
        assert other_track is not None
        other_track.recording_mbid = recording_mbid
        other_track.canonical_identity_verified = True
        other_job = session.scalar(
            select(DownloadJob).where(DownloadJob.request_track_id == other_track.id)
        )
        assert other_job is not None
        other_job.dedup_key = dedup_key(other_track)
    corrected = {
        **snapshot,
        "artist": "Gabry Ponte & KEL",
        "title": "Tarantella",
        "album": "Tarantella - Single",
        "version_signature": "studio",
        "recording_mbid": recording_mbid,
        "canonical_identity_verified": True,
    }

    with pytest.raises(EquivalentAcquisitionActive):
        processor._persist_resolved_identity(lease, corrected)

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        assert track.version_signature == "live"
        assert track.album == "Battiti Live Compilation 2024"
        assert job.dedup_key == old_key
        assert json.loads(job.approved_snapshot_json)["version_signature"] == "live"
    assert lease.approved_snapshot["version_signature"] == "live"
