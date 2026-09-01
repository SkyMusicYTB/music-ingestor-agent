from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.clients.ytdlp import DownloadCancelled, SourceValidationError, YtDlpError
from app.db.models import Conversation, Event, Request, RequestTrack, ServiceTask, User
from app.services.source_selection import SourceCandidate
from app.tools.youtube import YouTubeSearchArguments, YouTubeSearchResponse
from app.workers.queue import ServiceTaskQueue
from app.workers.service_tasks import WorkerServiceTaskHandler


class _FakeYtDlp:
    def validate_url(self, value: str) -> str:
        return value

    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        return {
            "id": "dQw4w9WgXcQ",
            "extractor": "youtube",
            "track": "Resolved Song",
            "artist": "Resolved Artist",
            "album": "Resolved Album",
            "duration": 180,
        }


class _RejectingYtDlp(_FakeYtDlp):
    def validate_url(self, value: str) -> str:
        raise SourceValidationError(f"rejected source: {value[:8]}")


class _UnavailableYtDlp(_FakeYtDlp):
    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        raise YtDlpError("temporary provider failure")


class _CancelledYtDlp(_FakeYtDlp):
    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        assert cancel_signal is not None and cancel_signal.is_set()
        raise DownloadCancelled("worker shutdown cancelled yt-dlp")


class _FakeYouTube:
    def search(self, query: str, *, limit: int, cancel_signal=None):
        return YouTubeSearchResponse(
            query=query,
            candidates=(
                SourceCandidate(
                    source_id="dQw4w9WgXcQ",
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title="Resolved Artist - Resolved Song",
                    channel="Resolved Artist - Topic",
                    duration_seconds=180,
                ),
            )[:limit],
        )


class _UnusedScanner:
    def run(self, *, full: bool, cancel_signal=None):
        raise AssertionError(f"unexpected scan full={full}")


class _CompletedScanner:
    def __init__(self, music_root: Path) -> None:
        self.music_root = music_root

    def run(self, *, full: bool, cancel_signal=None):
        assert not full
        return SimpleNamespace(
            id="scan-id",
            kind="incremental",
            status="completed",
            scanned_files=1,
            changed_files=1,
            error_count=0,
        )


class _RecordingDownloadQueue:
    def __init__(self) -> None:
        self.roots: list[Path] = []

    def adopt_published_jobs(self, music_root: Path) -> int:
        self.roots.append(music_root)
        return 1


def _request(session_factory, *, suffix: str) -> str:
    with session_factory.begin() as session:
        user = User(
            username=f"worker-{suffix}",
            username_normalized=f"worker-{suffix}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="direct")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="https://youtu.be/dQw4w9WgXcQ",
            action="add",
            input_kind="youtube_url",
            idempotency_key=f"worker-task-{suffix}",
        )
        session.add(request)
        session.flush()
        return request.id


def _handler(session_factory, queue: ServiceTaskQueue) -> WorkerServiceTaskHandler:
    return WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_FakeYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )


def test_direct_request_task_is_resolved_without_arbitrary_dispatch(session_factory) -> None:
    request_id = _request(session_factory, suffix="direct")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    outcome = _handler(session_factory, queue).process(lease)
    assert outcome.completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        track = session.scalar(select(RequestTrack).where(RequestTrack.request_id == request_id))
        confirmation = session.scalar(
            select(ServiceTask).where(
                ServiceTask.target == "web",
                ServiceTask.kind == "confirm_request",
            )
        )
        assert request is not None and request.status == "preview"
        assert track is not None
        assert track.artist == "Resolved Artist"
        assert track.source_id == "dQw4w9WgXcQ"
        assert confirmation is not None and confirmation.state == "queued"
        assert json.loads(confirmation.payload_json) == {"request_id": request_id}


def test_youtube_search_task_is_bounded_and_serialized(session_factory) -> None:
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="youtube_search",
            payload_json=json.dumps({"query": "Resolved Artist Song", "limit": 8}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    assert _handler(session_factory, queue).process(lease).completed
    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        assert task is not None and task.state == "completed"
        result = json.loads(task.result_json)
        assert result["candidates"][0]["source_id"] == "dQw4w9WgXcQ"


def test_youtube_search_tool_schema_requires_all_arguments() -> None:
    schema = YouTubeSearchArguments.model_json_schema()
    assert set(schema["required"]) == {"query", "limit"}


def test_periodic_library_scan_queue_is_idempotent(session_factory) -> None:
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    first = queue.ensure_scheduled_library_scan()
    second = queue.ensure_scheduled_library_scan()
    assert second == first
    with session_factory() as session:
        task = session.get(ServiceTask, first)
        assert task is not None
        assert task.kind == "library_scan"
        assert task.state == "queued"
        assert json.loads(task.payload_json) == {"full": False, "scheduled": True}


def test_completed_library_scan_reconciles_published_jobs(session_factory, tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    downloads = _RecordingDownloadQueue()
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="library_scan",
            payload_json=json.dumps({"full": False}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_FakeYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_CompletedScanner(music_root),  # type: ignore[arg-type]
        max_duration_seconds=600,
        download_queue=downloads,  # type: ignore[arg-type]
    )

    assert handler.process(lease).completed

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        assert task is not None
        assert json.loads(task.result_json)["reconciled_jobs"] == 1
    assert downloads.roots == [music_root]


def test_terminal_direct_validation_failure_marks_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="invalid")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_RejectingYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    assert not handler.process(lease).completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        event = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert request is not None and request.status == "failed"
        assert request.error_code == "invalid_source_url"
        assert event is not None


def test_exhausted_direct_probe_retries_mark_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="exhausted")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
                attempts=3,
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None and lease.attempts == 4
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_UnavailableYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    assert not handler.process(lease).completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        assert request is not None and request.status == "failed"
        assert request.error_code == "source_resolution_failed"


def test_planned_shutdown_releases_direct_task_without_attempt_or_request_failure(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="shutdown")
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="resolve_direct_request",
            payload_json=json.dumps({"request_id": request_id}),
            available_at=datetime.now(UTC),
            attempts=3,
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None and lease.attempts == 4
    shutdown = threading.Event()
    shutdown.set()
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_CancelledYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
        shutdown_signal=shutdown,
    )

    assert not handler.process(lease).completed

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        request = session.get(Request, request_id)
        failure = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert task is not None and task.state == "retry_wait"
        assert task.attempts == 3 and task.lease_token is None
        assert request is not None and request.status == "pending"
        assert failure is None


def test_expired_final_direct_task_marks_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="expired")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="resolve_direct_request",
            payload_json=json.dumps({"request_id": request_id}),
            state="running",
            attempts=4,
            lease_token="expired-token",  # noqa: S106 - inert fencing-token fixture
            lease_expires_at=now - timedelta(seconds=1),
        )
        session.add(task)
        session.flush()
        task_id = task.id

    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    assert queue.recover_expired(now=now) == 1

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        request = session.get(Request, request_id)
        event = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert task is not None and task.state == "failed"
        assert request is not None and request.status == "failed"
        assert request.error_code == "direct_resolution_failed"
        assert event is not None
