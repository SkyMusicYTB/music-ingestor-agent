from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
)
from app.db.models import Event, Request, RequestTrack, ServiceTask
from app.services.library_scan import LibraryScanner
from app.tools.youtube import YouTubeTool
from app.workers.queue import (
    DownloadJobQueue,
    LeaseLostError,
    ServiceTaskLease,
    ServiceTaskQueue,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceTaskOutcome:
    task_id: str
    kind: str
    completed: bool


class WorkerServiceTaskHandler:
    """Fixed allowlist for non-download work assigned to the worker service."""

    def __init__(
        self,
        *,
        queue: ServiceTaskQueue,
        factory: sessionmaker[Session],
        ytdlp: YtDlpClient,
        youtube: YouTubeTool,
        scanner: LibraryScanner,
        max_duration_seconds: int,
        shutdown_signal: CancellationSignal | None = None,
        download_queue: DownloadJobQueue | None = None,
    ) -> None:
        self.queue = queue
        self.factory = factory
        self.ytdlp = ytdlp
        self.youtube = youtube
        self.scanner = scanner
        self.max_duration_seconds = max_duration_seconds
        self.shutdown_signal = shutdown_signal
        self.download_queue = download_queue

    def process(self, lease: ServiceTaskLease) -> ServiceTaskOutcome:
        monitor = _ServiceLeaseMonitor(self.queue, lease)
        try:
            with monitor:
                result = self._dispatch(lease)
                monitor.raise_if_lost()
            self.queue.complete(lease, result)
            return ServiceTaskOutcome(lease.task_id, lease.kind, True)
        except LeaseLostError:
            return ServiceTaskOutcome(lease.task_id, lease.kind, False)
        except Exception as exc:
            monitor.stop()
            if (
                self.shutdown_signal is not None
                and self.shutdown_signal.is_set()
                and isinstance(exc, (DownloadCancelled, InterruptedError))
            ):
                try:
                    self.queue.release_for_shutdown(lease)
                except LeaseLostError:
                    pass
                return ServiceTaskOutcome(lease.task_id, lease.kind, False)
            retryable = isinstance(exc, (YtDlpError, OSError)) and not isinstance(
                exc, SourceValidationError
            )
            safe_error = _safe_task_error(exc)
            terminal = self.queue.fail(
                lease,
                safe_error,
                retryable=retryable,
            )
            if terminal and lease.kind == "resolve_direct_request":
                self._fail_direct_request(lease.payload, exc, safe_error)
            logger.warning("worker service task %s failed", lease.kind)
            return ServiceTaskOutcome(lease.task_id, lease.kind, False)

    def _dispatch(self, lease: ServiceTaskLease) -> dict[str, Any]:
        if lease.payload_version != 1:
            raise ValueError("unsupported worker task payload version")
        if lease.kind == "resolve_direct_request":
            return self._resolve_direct_request(lease.payload)
        if lease.kind in {"youtube_search", "search_youtube"}:
            return self._youtube_search(lease.payload)
        if lease.kind == "library_scan":
            full = lease.payload.get("full", False)
            if not isinstance(full, bool):
                raise ValueError("library_scan.full must be a boolean")
            scan = self.scanner.run(full=full, cancel_signal=self.shutdown_signal)
            result = {
                "scan_id": scan.id,
                "kind": scan.kind,
                "status": scan.status,
                "scanned_files": scan.scanned_files,
                "changed_files": scan.changed_files,
                "error_count": scan.error_count,
            }
            if self.download_queue is not None:
                result["reconciled_jobs"] = self.download_queue.adopt_published_jobs(
                    self.scanner.music_root
                )
            return result
        raise ValueError(f"unsupported worker service task kind: {lease.kind}")

    def _resolve_direct_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("resolve_direct_request requires request_id")
        with self.factory() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise ValueError("direct request was not found")
            raw_url = request.raw_text.strip()
            existing = session.scalar(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
                .limit(1)
            )
            if existing is not None:
                track_id = existing.id
            else:
                track_id = None

        if track_id is not None:
            with self.factory.begin() as session:
                _ensure_confirmation_task(session, request_id)
            return {"request_id": request_id, "request_track_id": track_id, "reused": True}

        validated = self.ytdlp.validate_url(raw_url)
        metadata = self.ytdlp.probe(validated, cancel_signal=self.shutdown_signal)
        duration = _positive_float(metadata.get("duration"))
        if duration is not None and duration > self.max_duration_seconds:
            raise ValueError("direct media exceeds the configured duration limit")
        source_id = _string(metadata.get("id"))
        if source_id is None:
            raise ValueError("YouTube metadata did not contain a source ID")
        title = _string(metadata.get("track")) or _string(metadata.get("title"))
        artist = (
            _string(metadata.get("artist"))
            or _string(metadata.get("creator"))
            or _string(metadata.get("uploader"))
            or _string(metadata.get("channel"))
        )
        if title is None or artist is None:
            raise ValueError("YouTube metadata did not contain artist and title")
        if artist.casefold().endswith(" - topic"):
            artist = artist[:-8].strip()
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise ValueError("direct request disappeared")
            existing = session.scalar(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
                .limit(1)
            )
            if existing is None:
                existing = RequestTrack(
                    request_id=request_id,
                    ordinal=1,
                    artist=artist[:300],
                    title=title[:300],
                    album=(_string(metadata.get("album")) or "")[:300] or None,
                    album_artist=artist[:300],
                    duration_seconds=duration,
                    source_url=validated,
                    source_extractor="youtube",
                    source_id=source_id[:100],
                    version_signature="studio",
                    rationale="Resolved directly from the reviewed YouTube URL.",
                    evidence_json=json.dumps(["yt-dlp metadata"], separators=(",", ":")),
                    metadata_confidence=0.80,
                    selected=True,
                )
                session.add(existing)
                session.flush()
            request.discovered_count = 1
            request.status = "preview"
            _ensure_confirmation_task(session, request_id)
            session.add(
                Event(
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.direct_resolved",
                    message="Direct YouTube request resolved for review",
                )
            )
            track_id = existing.id
        return {"request_id": request_id, "request_track_id": track_id, "reused": False}

    def _youtube_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = payload.get("query")
        if not isinstance(query, str):
            raise ValueError("youtube_search requires a query")
        limit = payload.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("youtube_search limit must be between 1 and 8")
        response = self.youtube.search(query, limit=limit, cancel_signal=self.shutdown_signal)
        return {
            "query": response.query,
            "candidates": [
                {
                    "source_id": item.source_id,
                    "source_extractor": item.extractor,
                    "url": item.url,
                    "title": item.title,
                    "channel": item.channel,
                    "duration_seconds": item.duration_seconds,
                }
                for item in response.candidates
            ],
        }

    def _fail_direct_request(
        self,
        payload: dict[str, Any],
        error: Exception,
        safe_message: str,
    ) -> None:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        error_code = (
            "invalid_source_url"
            if isinstance(error, SourceValidationError)
            else "source_resolution_failed"
        )
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None or request.status in {"auto_queued", "queued"}:
                return
            request.status = "failed"
            request.error_code = error_code
            request.error_message = safe_message[:500]
            session.add(
                Event(
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.direct_failed",
                    message="Direct YouTube request could not be resolved",
                )
            )


class _ServiceLeaseMonitor:
    def __init__(self, queue: ServiceTaskQueue, lease: ServiceTaskLease) -> None:
        self.queue = queue
        self.lease = lease
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: LeaseLostError | None = None

    def __enter__(self) -> _ServiceLeaseMonitor:
        self.thread = threading.Thread(target=self._run, name="service-task-lease", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def raise_if_lost(self) -> None:
        if self.error is not None:
            raise self.error

    def _run(self) -> None:
        interval = max(2.0, min(20.0, self.queue.lease_seconds / 3))
        while not self.stop_event.wait(interval):
            try:
                self.queue.heartbeat(self.lease)
            except LeaseLostError as exc:
                self.error = exc
                return


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result > 0 else None


def _safe_task_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, SourceValidationError)):
        return str(exc)[:1000]
    return f"{type(exc).__name__}: worker task failed"


def _ensure_confirmation_task(session: Session, request_id: str) -> None:
    active = session.scalar(
        select(ServiceTask.id)
        .where(
            ServiceTask.target == "web",
            ServiceTask.kind == "confirm_request",
            ServiceTask.state.in_(["queued", "running", "retry_wait"]),
            ServiceTask.payload_json
            == json.dumps({"request_id": request_id}, separators=(",", ":")),
        )
        .limit(1)
    )
    if active is not None:
        return
    session.add(
        ServiceTask(
            target="web",
            kind="confirm_request",
            payload_json=json.dumps({"request_id": request_id}, separators=(",", ":")),
            available_at=datetime.now(UTC),
        )
    )
