from __future__ import annotations

import threading
from pathlib import Path

import pytest

from app.services.filesystem import create_staging_directory
from app.services.library_scan import LibraryScanner
from app.workers.cleanup import cleanup_orphaned_staging
from app.workers.queue import DownloadJobQueue, ServiceTaskQueue
from app.workers.recovery import StartupRecovery


def test_startup_cleanup_preserves_resumable_job_and_removes_stale_uuid(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    retained_id = "11111111-1111-1111-1111-111111111111"
    stale_id = "22222222-2222-2222-2222-222222222222"
    retained = create_staging_directory(root, retained_id)
    stale = create_staging_directory(root, stale_id)
    (retained / "source.webm.part").write_bytes(b"resume")
    (stale / "source.webm.part").write_bytes(b"stale")

    removed = cleanup_orphaned_staging(root, preserve_job_ids={retained_id})

    assert removed == 1
    assert retained.is_dir() and (retained / "source.webm.part").exists()
    assert not stale.exists()


def test_startup_recovery_scan_honors_worker_shutdown(session_factory, settings) -> None:
    shutdown = threading.Event()
    shutdown.set()
    recovery = StartupRecovery(
        downloads=DownloadJobQueue(session_factory, lease_seconds=30),
        service_tasks=ServiceTaskQueue(
            session_factory,
            target="worker",
            lease_seconds=30,
        ),
        scanner=LibraryScanner(session_factory, settings.music_path),
        downloads_path=settings.downloads_path,
        shutdown_signal=shutdown,
    )

    with pytest.raises(InterruptedError, match="worker shutdown"):
        recovery.run()
