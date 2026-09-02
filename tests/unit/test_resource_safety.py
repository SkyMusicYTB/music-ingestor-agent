from __future__ import annotations

import json
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Conversation,
    DownloadJob,
    JobArtifact,
    Request,
    RequestTrack,
    User,
)
from app.services.filesystem import InsufficientSpaceError
from app.workers.process import (
    BoundedProcessResult,
    FrameCallback,
    ProcessFrameLimitExceeded,
    ProcessOutputLimitExceeded,
    ProcessTimedOut,
    run_bounded_process,
)
from app.workers.reservations import MediaReservationManager, ReservationLost


def _python_process(
    source: str,
    *,
    timeout: float = 3.0,
    stdout_limit: int = 1024 * 1024,
    stderr_limit: int = 1024 * 1024,
    frame_limit: int = 256 * 1024,
    on_frame: FrameCallback | None = None,
) -> BoundedProcessResult:
    return run_bounded_process(
        [sys.executable, "-c", source],
        environment={},
        timeout_seconds=timeout,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        frame_limit=frame_limit,
        on_frame=on_frame,
    )


def test_giant_newline_free_frame_is_rejected_and_process_group_is_stopped(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = (
        f"import pathlib,time;time.sleep(0.5);pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    source = (
        "import os,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        "os.write(1,b'x'*131072);time.sleep(5)"
    )

    with pytest.raises(ProcessFrameLimitExceeded, match="stdout"):
        _python_process(
            source,
            frame_limit=64 * 1024,
            on_frame=lambda _stream, _frame: None,
        )

    time.sleep(0.7)
    assert not marker.exists(), "overflow must terminate descendants in the child process group"


def test_simultaneous_stdout_and_stderr_overflow_never_deadlocks() -> None:
    source = """
import os
import threading
import time

barrier = threading.Barrier(3)
def flood(fd):
    barrier.wait()
    for _ in range(32):
        os.write(fd, b'x' * 32768)

threads = [threading.Thread(target=flood, args=(1,)), threading.Thread(target=flood, args=(2,))]
for thread in threads:
    thread.start()
barrier.wait()
for thread in threads:
    thread.join()
time.sleep(5)
"""

    started = time.monotonic()
    with pytest.raises(ProcessOutputLimitExceeded) as error:
        _python_process(source, stdout_limit=64 * 1024, stderr_limit=64 * 1024)
    assert error.value.stream in {"stdout", "stderr"}
    assert time.monotonic() - started < 2.0


def test_output_limits_count_raw_multibyte_bytes_not_characters() -> None:
    source = "import os;os.write(1,('é'*4).encode('utf-8'))"

    result = _python_process(source, stdout_limit=8)
    assert result.stdout == "éééé".encode()
    with pytest.raises(ProcessOutputLimitExceeded, match="stdout"):
        _python_process(source, stdout_limit=7)


def test_timeout_stops_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "timeout-grandchild-survived"
    grandchild = (
        f"import pathlib,time;time.sleep(0.5);pathlib.Path({str(marker)!r}).write_text('alive')"
    )
    source = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        "time.sleep(5)"
    )

    with pytest.raises(ProcessTimedOut):
        _python_process(source, timeout=0.15)

    time.sleep(0.7)
    assert not marker.exists(), "timeout must terminate descendants in the child process group"


def _add_active_job(factory: sessionmaker[Session], suffix: str) -> tuple[str, str]:
    lease_token = secrets.token_hex(32)
    with factory.begin() as session:
        user = User(
            username=f"resource-{suffix}",
            username_normalized=f"resource-{suffix}",
            password_hash="fixture-hash",  # noqa: S106 - inert database fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="resource test")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add fixture",
            action="add",
            idempotency_key=f"resource-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Artist",
            title=f"Track {suffix}",
        )
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps({"artist": "Artist", "title": track.title}),
            dedup_key=f"resource:{suffix}",
            status="active",
            stage="downloading",
            lease_token=lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        return job.id, lease_token


@pytest.fixture
def fixed_free_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.workers.reservations.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=500),
    )


def test_concurrent_same_filesystem_reservations_enforce_aggregate_headroom(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    fixed_free_space: None,
) -> None:
    jobs = [_add_active_job(session_factory, str(index)) for index in range(3)]
    roots = [tmp_path / f"stage-{index}" for index in range(3)]
    for root in roots:
        root.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=100,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    barrier = threading.Barrier(3)

    def reserve(pair: tuple[tuple[str, str], Path]) -> str:
        barrier.wait()
        (job_id, lease_token), root = pair
        try:
            return manager.reserve_download(job_id, lease_token, root).filesystem_key
        except InsufficientSpaceError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(reserve, zip(jobs, roots, strict=True)))

    assert results.count("rejected") == 1
    filesystem_keys = {value for value in results if value != "rejected"}
    assert filesystem_keys == {f"device:{tmp_path.stat().st_dev}"}
    with session_factory() as session:
        active = session.scalars(select(JobArtifact).where(JobArtifact.status == "creating")).all()
    assert len(active) == 2
    assert sum(int(item.size_bytes or 0) for item in active) == 400


def test_stale_reservation_is_reclaimed_and_retained_bytes_reduce_new_headroom(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    fixed_free_space: None,
) -> None:
    stale_job, stale_token = _add_active_job(session_factory, "stale")
    other_job, other_token = _add_active_job(session_factory, "other")
    stale_root = tmp_path / "stale-stage"
    other_root = tmp_path / "other-stage"
    stale_root.mkdir()
    other_root.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=50,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    stale_reservation = manager.reserve_download(stale_job, stale_token, stale_root)
    (stale_root / "retained.part").write_bytes(b"x" * 60)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, stale_job)
        assert job is not None
        job.status = "retry_wait"

    manager.reserve_download(other_job, other_token, other_root)
    with session_factory() as session:
        stale_artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == stale_job,
                JobArtifact.kind == stale_reservation.kind,
            )
        )
        assert stale_artifact is not None
        assert stale_artifact.status == "removed"
        assert stale_artifact.size_bytes == 0

    resumed_token = secrets.token_hex(32)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, stale_job)
        assert job is not None
        job.status = "active"
        job.lease_token = resumed_token
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    resumed = manager.reserve_download(stale_job, resumed_token, stale_root)
    with session_factory() as session:
        resumed_artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == stale_job,
                JobArtifact.kind == resumed.kind,
            )
        )
        assert resumed_artifact is not None
        assert resumed_artifact.status == "creating"
        assert resumed_artifact.size_bytes == 140


def test_release_is_idempotent_and_returns_reserved_headroom(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    fixed_free_space: None,
) -> None:
    job_id, lease_token = _add_active_job(session_factory, "release")
    staging = tmp_path / "release-stage"
    staging.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=100,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    reservation = manager.reserve_download(job_id, lease_token, staging)

    manager.release(reservation)
    manager.release(reservation)

    with session_factory() as session:
        artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == job_id,
                JobArtifact.kind == reservation.kind,
            )
        )
        assert artifact is not None
        assert artifact.status == "removed"
        assert artifact.size_bytes == 0


def test_stale_reservation_cannot_release_or_update_replacement_generation(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.workers.reservations.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=300),
    )
    job_id, lease_token = _add_active_job(session_factory, "reservation-generation")
    other_job_id, other_lease_token = _add_active_job(
        session_factory, "reservation-generation-other"
    )
    staging = tmp_path / "reservation-generation"
    other_staging = tmp_path / "reservation-generation-other"
    staging.mkdir()
    other_staging.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=100,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    stale = manager.reserve_download(job_id, lease_token, staging)
    replacement = manager.reserve_download(job_id, lease_token, staging)
    assert stale.generation_token != replacement.generation_token

    manager.release(stale)
    with pytest.raises(ReservationLost):
        manager.update_download(stale, staging)
    assert manager.update_download(replacement, staging) == 0

    with pytest.raises(InsufficientSpaceError):
        manager.reserve_download(other_job_id, other_lease_token, other_staging)
    with session_factory() as session:
        artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == job_id,
                JobArtifact.kind == replacement.kind,
            )
        )
        assert artifact is not None
        assert artifact.status == "creating"
        assert artifact.generation_token == replacement.generation_token
        assert artifact.size_bytes == 200


def test_stale_job_lease_cannot_replace_the_live_workers_reservation(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    fixed_free_space: None,
) -> None:
    job_id, stale_token = _add_active_job(session_factory, "reservation-lease-fence")
    staging = tmp_path / "reservation-lease-fence"
    staging.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=100,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    stale = manager.reserve_download(job_id, stale_token, staging)
    replacement_token = secrets.token_hex(32)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.lease_token = replacement_token
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    replacement = manager.reserve_download(job_id, replacement_token, staging)
    with pytest.raises(ReservationLost, match="lease"):
        manager.reserve_download(job_id, stale_token, staging)
    manager.release(stale)
    assert manager.update_download(replacement, staging) == 0

    with session_factory() as session:
        artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == job_id,
                JobArtifact.kind == replacement.kind,
            )
        )
        assert artifact is not None
        assert artifact.status == "creating"
        assert artifact.generation_token == replacement.generation_token


def test_expired_active_lease_reservation_is_reclaimed_and_cannot_update(
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    fixed_free_space: None,
) -> None:
    stale_job, stale_token = _add_active_job(session_factory, "expired-reservation")
    live_job, live_token = _add_active_job(session_factory, "live-after-expiry")
    stale_root = tmp_path / "expired-reservation"
    live_root = tmp_path / "live-after-expiry"
    stale_root.mkdir()
    live_root.mkdir()
    manager = MediaReservationManager(
        session_factory,
        min_free_bytes=100,
        max_media_bytes=100,
        normalization_overhead_bytes=0,
    )
    stale = manager.reserve_download(stale_job, stale_token, stale_root)
    with session_factory.begin() as session:
        job = session.get(DownloadJob, stale_job)
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ReservationLost, match="lease"):
        manager.update_download(stale, stale_root)
    manager.reserve_download(live_job, live_token, live_root)

    with session_factory() as session:
        artifact = session.scalar(
            select(JobArtifact).where(
                JobArtifact.job_id == stale_job,
                JobArtifact.kind == stale.kind,
            )
        )
        assert artifact is not None
        assert artifact.status == "removed"
        assert artifact.size_bytes == 0
