from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from app.clients.ytdlp import YtDlpClient
from app.config import Settings, get_settings
from app.db.engine import (
    assert_database_pragmas,
    assert_schema_current,
    create_database_engine,
    make_session_factory,
)
from app.logging import configure_logging
from app.services.artwork import ArtworkCacheService, ArtworkFetcher
from app.services.library_scan import LibraryScanner
from app.tools.youtube import YouTubeTool
from app.workers.download_pipeline import DownloadPipeline
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.media import MediaProcessor
from app.workers.metadata import MusicBrainzWorkerResolver
from app.workers.processor import DownloadJobProcessor, ProcessOutcome
from app.workers.queue import DownloadJobQueue, ServiceTaskQueue
from app.workers.recovery import StartupRecovery
from app.workers.service_tasks import ServiceTaskOutcome, WorkerServiceTaskHandler

logger = logging.getLogger(__name__)


class WorkerRunner:
    def __init__(
        self,
        processor: DownloadJobProcessor,
        queue: DownloadJobQueue,
        *,
        slots: int,
        poll_seconds: float = 1.0,
        service_queue: ServiceTaskQueue | None = None,
        service_handler: WorkerServiceTaskHandler | None = None,
        recovery: StartupRecovery | None = None,
        heartbeat: WorkerHeartbeat | None = None,
        stop_event: threading.Event | None = None,
        metadata_resolver: MusicBrainzWorkerResolver | None = None,
        scan_interval_seconds: float = 30 * 60,
    ) -> None:
        if slots <= 0:
            raise ValueError("worker slots must be positive")
        if scan_interval_seconds < 60:
            raise ValueError("scan interval must be at least 60 seconds")
        self.processor = processor
        self.queue = queue
        self.slots = slots
        self.poll_seconds = poll_seconds
        self.service_queue = service_queue
        self.service_handler = service_handler
        self.recovery = recovery
        self.heartbeat = heartbeat
        self.stop_event = stop_event or threading.Event()
        self.metadata_resolver = metadata_resolver
        self.scan_interval_seconds = scan_interval_seconds

    def run_forever(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.start()
        try:
            if self.recovery is not None:
                report = self.recovery.run()
                logger.info(
                    "startup recovery completed: %s leases, %s tasks, %s adopted, "
                    "%s staging directories",
                    report.download_leases,
                    report.service_task_leases,
                    report.adopted_publications,
                    report.staging_directories,
                )
            else:
                self.queue.recover_expired()
            self._run_executors()
        finally:
            if self.heartbeat is not None:
                self.heartbeat.stop()
            if self.metadata_resolver is not None:
                self.metadata_resolver.close()

    def _run_executors(self) -> None:
        futures: set[Future[ProcessOutcome]] = set()
        service_future: Future[ServiceTaskOutcome] | None = None
        recovery_at = time.monotonic() + 30
        scan_at = time.monotonic() + self.scan_interval_seconds
        with (
            ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="download") as executor,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="worker-task") as task_executor,
        ):
            while not self.stop_event.is_set():
                finished = {future for future in futures if future.done()}
                for future in finished:
                    futures.remove(future)
                    self._report_download_future(future)
                if service_future is not None and service_future.done():
                    self._report_service_future(service_future)
                    service_future = None
                while len(futures) < self.slots and not self.stop_event.is_set():
                    lease = self.queue.claim_next()
                    if lease is None:
                        break
                    futures.add(executor.submit(self.processor.process, lease))
                if (
                    service_future is None
                    and self.service_queue is not None
                    and self.service_handler is not None
                ):
                    service_lease = self.service_queue.claim_next()
                    if service_lease is not None:
                        service_future = task_executor.submit(
                            self.service_handler.process, service_lease
                        )
                if time.monotonic() >= recovery_at:
                    self.queue.recover_expired()
                    if self.service_queue is not None:
                        self.service_queue.recover_expired()
                    recovery_at = time.monotonic() + 30
                if self.service_queue is not None and time.monotonic() >= scan_at:
                    try:
                        task_id = self.service_queue.ensure_scheduled_library_scan()
                        logger.info(
                            "periodic incremental library scan is queued",
                            extra={"task_id": task_id},
                        )
                    except Exception:
                        logger.exception("could not queue periodic incremental library scan")
                    finally:
                        scan_at = time.monotonic() + self.scan_interval_seconds
                if self.heartbeat is not None:
                    self.heartbeat.set_active(len(futures) + int(service_future is not None))
                self.stop_event.wait(self.poll_seconds)
            for future in futures:
                self._report_download_future(future)
            if service_future is not None:
                self._report_service_future(service_future)

    @staticmethod
    def _report_download_future(future: Future[ProcessOutcome]) -> None:
        try:
            outcome = future.result()
            logger.info(
                "job finished with state %s",
                outcome.status,
                extra={"job_id": outcome.job_id},
            )
        except Exception:
            logger.exception("uncaught download-worker failure")

    @staticmethod
    def _report_service_future(future: Future[ServiceTaskOutcome]) -> None:
        try:
            outcome = future.result()
            logger.info("worker service task %s completed=%s", outcome.kind, outcome.completed)
        except Exception:
            logger.exception("uncaught worker service-task failure")

    def stop(self) -> None:
        self.stop_event.set()


def build_runner(settings: Settings) -> tuple[WorkerRunner, ArtworkFetcher]:
    engine = create_database_engine(settings)
    assert_database_pragmas(engine)
    assert_schema_current(engine)
    session_factory = make_session_factory(engine)
    queue = DownloadJobQueue(session_factory, lease_seconds=settings.lease_seconds)
    service_queue = ServiceTaskQueue(
        session_factory, target="worker", lease_seconds=settings.lease_seconds
    )
    ytdlp = YtDlpClient(
        source_policy=settings.media_source_policy,
        enabled_providers=settings.enabled_media_providers,
        allowed_hosts=settings.allowed_media_hosts,
        allowed_extractors=settings.allowed_media_extractors,
        blocked_extractors=settings.blocked_media_extractors,
        allow_generic_extractor=settings.allow_generic_extractor,
    )
    youtube = YouTubeTool(ytdlp, max_duration_seconds=settings.max_direct_media_seconds)
    media = MediaProcessor()
    artwork_fetcher = ArtworkFetcher()
    artwork_cache = ArtworkCacheService(
        session_factory,
        settings.artwork_path,
        artwork_fetcher,
    )
    scanner = LibraryScanner(session_factory, settings.music_path)
    metadata_resolver = MusicBrainzWorkerResolver(settings)
    stop_event = threading.Event()
    processor = DownloadPipeline(
        settings=settings,
        queue=queue,
        ytdlp=ytdlp,
        youtube=youtube,
        artwork_fetcher=artwork_cache,
        media=media,
        session_factory=session_factory,
        library_scanner=scanner,
        shutdown_signal=stop_event,
        metadata_resolver=metadata_resolver,
    )
    service_handler = WorkerServiceTaskHandler(
        queue=service_queue,
        factory=session_factory,
        ytdlp=ytdlp,
        youtube=youtube,
        scanner=scanner,
        max_duration_seconds=settings.max_direct_media_seconds,
        shutdown_signal=stop_event,
        download_queue=queue,
        source_probe_negative_ttl_seconds=settings.source_probe_negative_ttl_seconds,
        enabled_providers=settings.enabled_media_providers,
        max_direct_playlist_items=settings.max_direct_playlist_items,
    )
    recovery = StartupRecovery(
        downloads=queue,
        service_tasks=service_queue,
        scanner=scanner,
        downloads_path=settings.downloads_path,
        shutdown_signal=stop_event,
    )
    heartbeat = WorkerHeartbeat(session_factory, service_version=settings.app_version)
    return (
        WorkerRunner(
            processor,
            queue,
            slots=settings.worker_download_slots,
            service_queue=service_queue,
            service_handler=service_handler,
            recovery=recovery,
            heartbeat=heartbeat,
            stop_event=stop_event,
            metadata_resolver=metadata_resolver,
        ),
        artwork_fetcher,
    )


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="music-agent-worker",
        description="Run the durable Music Agent media worker.",
    )


def main(argv: list[str] | None = None) -> None:
    build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    runner, artwork_fetcher = build_runner(settings)

    def request_stop(_signum: int, _frame: object) -> None:
        runner.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        runner.run_forever()
    finally:
        artwork_fetcher.close()


if __name__ == "__main__":  # pragma: no cover
    main()
