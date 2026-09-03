from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Conversation, DownloadJob, Event, Request, RequestTrack, Track, User
from app.workers.queue import (
    DownloadJobQueue,
    JobCancellationRequested,
    JobLease,
    LeaseLostError,
)


def _add_job(
    session_factory: sessionmaker[Session],
    *,
    suffix: str,
    available_at: datetime,
    priority: int = 100,
) -> str:
    with session_factory.begin() as session:
        user = User(
            username=f"user-{suffix}",
            username_normalized=f"user-{suffix}",
            password_hash="hash",  # noqa: S106 - inert database fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="test")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add a track",
            action="add",
            idempotency_key=f"idem-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Artist",
            title=f"Song {suffix}",
        )
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps({"artist": "Artist", "title": f"Song {suffix}"}),
            dedup_key=f"youtube:{suffix}",
            available_at=available_at,
            priority=priority,
        )
        session.add(job)
        session.flush()
        return job.id


def test_claim_is_atomic_and_all_mutations_are_fenced(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="one", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None
    assert lease.job_id == job_id
    assert queue.claim_next(now=now) is None

    stale = JobLease(
        job_id=lease.job_id,
        token="stale-token",  # noqa: S106 - fencing-token fixture
        approved_snapshot=lease.approved_snapshot,
        retry_count=lease.retry_count,
    )
    with pytest.raises(LeaseLostError):
        queue.set_progress(stale, stage="downloading", progress=0.5, now=now)
    queue.set_progress(lease, stage="downloading", progress=0.5, now=now)


def test_initial_scan_wait_preserves_retry_budget_and_honors_cancel(session_factory):
    now = datetime.now(UTC)
    job_id = _add_job(session_factory, suffix="scan-wait", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next()
    assert queue.defer_for_library_scan(lease) == "queued"
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job.retry_count == 0 and job.lease_token is None
    lease = queue.claim_next(now=now + timedelta(seconds=60))
    queue.request_cancel(job_id)
    assert queue.defer_for_library_scan(lease) == "cancelled"


def test_processor_does_not_acquire_or_publish_without_initial_scan(
    session_factory, settings, monkeypatch, tmp_path
):
    from app.workers.processor import DownloadJobProcessor, InitialLibraryScanPending

    active_settings = settings.model_copy(update={"initial_scan_required": True})
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    _add_job(session_factory, suffix="scan-gate", available_at=datetime.now(UTC))
    processor = DownloadJobProcessor(
        settings=active_settings, queue=queue, ytdlp=object(), session_factory=session_factory
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("acquisition ran before the scan gate")

    monkeypatch.setattr(processor, "_acquire_valid_source", forbidden)
    lease = queue.claim_next()
    assert processor.process(lease).status == "queued"
    source = tmp_path / "source.mp3"
    source.write_bytes(b"audio")
    with pytest.raises(InitialLibraryScanPending):
        processor._publish_or_adopt(source, "Artist/Album/01 - Song.mp3", source_id="source")
    assert source.exists()
    assert not (settings.music_path / "Artist").exists()


def test_cancel_running_job_is_cooperative_and_fenced(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    _add_job(session_factory, suffix="cancel", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None
    assert queue.request_cancel(lease.job_id, now=now) == "cancel_requested"
    with pytest.raises(JobCancellationRequested):
        queue.heartbeat(lease, now=now)
    queue.acknowledge_cancel(lease, now=now)
    with pytest.raises(LeaseLostError):
        queue.heartbeat(lease, now=now)


def test_cancellation_after_atomic_publication_reconciles_as_completed(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="published-cancel", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None
    assert queue.request_cancel(job_id, now=now) == "cancel_requested"

    queue.complete(
        lease,
        final_relative_path="Artist/Album/01 - Published.opus",
        final_sha256="a" * 64,
        published=True,
        now=now,
    )

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and job.status == "completed"


def test_planned_shutdown_releases_active_lease_without_retry_penalty(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="shutdown", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None

    assert queue.release_for_shutdown(lease, now=now) == "retry_wait"

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        assert job.retry_count == 0 and job.lease_token is None
    assert queue.claim_next(now=now) is not None


def test_expired_lease_is_recovered_with_retry_backoff(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="recover", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    assert queue.claim_next(now=now) is not None
    assert queue.recover_expired(now=now + timedelta(seconds=31)) == 1
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        assert job.status == "retry_wait"
        assert job.retry_count == 1
        assert job.lease_token is None
        retry_at = (
            job.available_at.replace(tzinfo=UTC)
            if job.available_at.tzinfo is None
            else job.available_at
        )
        assert 12 <= (retry_at - (now + timedelta(seconds=31))).total_seconds() <= 18


def test_concurrent_claimers_receive_different_jobs(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    expected = {
        _add_job(session_factory, suffix="a", available_at=now),
        _add_job(session_factory, suffix="b", available_at=now),
    }
    queue = DownloadJobQueue(session_factory, lease_seconds=30)

    def claim() -> str | None:
        lease = queue.claim_next(now=now)
        return lease.job_id if lease else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = set(executor.map(lambda _value: claim(), range(2)))
    assert claimed == expected


def test_retry_budget_eventually_fails_job(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="retry", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None
    assert (
        queue.fail(
            lease,
            error_code="network",
            error_message="temporary",
            retryable=True,
            max_retries=0,
            now=now,
        )
        == "failed"
    )
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.lease_token is None


def test_job_warning_is_fenced_deduplicated_and_emits_event(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="warning", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None

    queue.add_warning(lease, code="fallback_art", message="Fallback artwork was used.")
    queue.add_warning(lease, code="fallback_art", message="Fallback artwork was used.")

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        events = list(
            session.scalars(
                select(Event).where(
                    Event.entity_id == job_id,
                    Event.event_type == "job.warning",
                )
            )
        )
        assert job is not None
        assert json.loads(job.warnings_json) == [
            {"code": "fallback_art", "message": "Fallback artwork was used."}
        ]
        assert len(events) == 2


def test_queue_progress_and_terminal_states_emit_job_events(session_factory) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="events", available_at=now)
    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    lease = queue.claim_next(now=now)
    assert lease is not None
    queue.set_progress(lease, stage="downloading", progress=0.25, now=now)
    queue.fail(
        lease,
        error_code="network",
        error_message="temporary",
        retryable=True,
        now=now,
    )
    with session_factory() as session:
        kinds = set(session.scalars(select(Event.event_type).where(Event.entity_id == job_id)))
    assert {"job.active", "job.progress", "job.retry_wait"} <= kinds


def test_startup_adopts_provenance_published_file(session_factory, tmp_path: Path) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="adopt", available_at=now)
    music_root = tmp_path / "music"
    published = music_root / "Artist" / "Album" / "01 - Song.opus"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"already-published")
    file_stat = published.stat()
    with session_factory.begin() as session:
        session.add(
            Track(
                artist="Artist",
                artist_normalized="artist",
                title="Song",
                title_normalized="song",
                filepath="Artist/Album/01 - Song.opus",
                file_mtime_ns=file_stat.st_mtime_ns,
                file_size=file_stat.st_size,
                provenance_json=json.dumps({"job_id": job_id}),
            )
        )

    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    assert queue.adopt_published_jobs(music_root) == 1

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and job.status == "completed"
        assert job.final_relative_path == "Artist/Album/01 - Song.opus"
        assert job.final_sha256 is not None and len(job.final_sha256) == 64


def test_later_scan_relinks_completed_job_and_clears_pending_warning(
    session_factory, tmp_path: Path
) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix="later-index", available_at=now)
    music_root = tmp_path / "music-later-index"
    published = music_root / "Artist" / "Album" / "01 - Later.opus"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"published-before-index")
    file_stat = published.stat()
    relative = published.relative_to(music_root).as_posix()
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.status = "completed"
        job.stage = "completed"
        job.progress = 1.0
        job.completed_at = now
        job.final_relative_path = relative
        job.final_sha256 = "a" * 64
        job.warnings_json = json.dumps(
            [
                {
                    "code": "index_reconciliation_pending",
                    "message": "Published file will be reconciled by a later scan.",
                },
                {"code": "fallback_art", "message": "Fallback artwork was used."},
            ]
        )
        track = Track(
            artist="Artist",
            artist_normalized="artist",
            title="Later",
            title_normalized="later",
            filepath=relative,
            file_mtime_ns=file_stat.st_mtime_ns,
            file_size=file_stat.st_size,
            provenance_json=json.dumps({"job_id": job_id}),
        )
        session.add(track)
        session.flush()
        track_id = track.id

    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    assert queue.adopt_published_jobs(music_root) == 1

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        events = list(
            session.scalars(
                select(Event).where(
                    Event.entity_id == job_id,
                    Event.event_type == "job.reconciled",
                )
            )
        )
        assert job is not None and job.final_track_id == track_id
        assert json.loads(job.warnings_json) == [
            {"code": "fallback_art", "message": "Fallback artwork was used."}
        ]
        assert len(events) == 1


@pytest.mark.parametrize("status", ["active", "failed", "cancelled"])
def test_startup_adopts_publication_regardless_of_stale_terminal_or_lease_state(
    session_factory, tmp_path: Path, status: str
) -> None:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    job_id = _add_job(session_factory, suffix=f"adopt-{status}", available_at=now)
    music_root = tmp_path / f"music-{status}"
    published = music_root / "Artist" / "Album" / f"01 - {status}.opus"
    published.parent.mkdir(parents=True)
    published.write_bytes(f"published-{status}".encode())
    file_stat = published.stat()
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.status = status
        if status == "active":
            job.lease_token = "unexpired-token"  # noqa: S105 - inert fencing-token fixture
            job.lease_expires_at = now + timedelta(hours=1)
        session.add(
            Track(
                artist="Artist",
                artist_normalized="artist",
                title=status,
                title_normalized=status,
                filepath=published.relative_to(music_root).as_posix(),
                file_mtime_ns=file_stat.st_mtime_ns,
                file_size=file_stat.st_size,
                provenance_json=json.dumps({"job_id": job_id}),
            )
        )

    queue = DownloadJobQueue(session_factory, lease_seconds=30)
    assert queue.adopt_published_jobs(music_root) == 1
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and job.status == "completed"
        assert job.lease_token is None and job.final_relative_path is not None
