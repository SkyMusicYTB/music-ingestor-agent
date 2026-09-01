from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.library_scan import LibraryScanner, ScanCancellation
from app.workers.cleanup import cleanup_orphaned_staging
from app.workers.queue import DownloadJobQueue, ServiceTaskQueue


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    download_leases: int
    service_task_leases: int
    adopted_publications: int
    staging_directories: int
    scan_kind: str


class StartupRecovery:
    def __init__(
        self,
        *,
        downloads: DownloadJobQueue,
        service_tasks: ServiceTaskQueue,
        scanner: LibraryScanner,
        downloads_path: Path,
        shutdown_signal: ScanCancellation | None = None,
    ) -> None:
        self.downloads = downloads
        self.service_tasks = service_tasks
        self.scanner = scanner
        self.downloads_path = downloads_path
        self.shutdown_signal = shutdown_signal

    def run(self) -> RecoveryReport:
        recovered_downloads = self.downloads.recover_expired()
        recovered_tasks = self.service_tasks.recover_expired()
        scan = self.scanner.run(full=False, cancel_signal=self.shutdown_signal)
        adopted = self.downloads.adopt_published_jobs(self.scanner.music_root)
        preserved = self.downloads.staging_job_ids_to_preserve()
        removed = cleanup_orphaned_staging(
            self.downloads_path,
            preserve_job_ids=preserved,
        )
        return RecoveryReport(
            download_leases=recovered_downloads,
            service_task_leases=recovered_tasks,
            adopted_publications=adopted,
            staging_directories=removed,
            scan_kind=scan.kind,
        )
