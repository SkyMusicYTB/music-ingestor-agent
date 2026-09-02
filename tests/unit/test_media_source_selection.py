from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.clients.ytdlp import DownloadCancelled
from app.db.models import Conversation, DownloadJob, Request, RequestTrack, ServiceTask, User
from app.services.source_selection import SourceCandidate, TrackIntent, select_source
from app.workers.processor import DownloadJobProcessor, SourceNeedsReview
from app.workers.queue import JobLease


def _candidate(source_id: str, title: str, *, duration: float = 180) -> SourceCandidate:
    return SourceCandidate(
        source_id=source_id,
        url=f"https://www.youtube.com/watch?v={source_id}",
        title=title,
        channel="Example Artist - Topic",
        duration_seconds=duration,
    )


def test_source_selection_penalizes_unrequested_versions() -> None:
    decision = select_source(
        TrackIntent(artist="Example Artist", title="My Song", duration_seconds=180),
        [
            _candidate("aaaaaaaaaaa", "Example Artist - My Song (Live)"),
            _candidate("bbbbbbbbbbb", "Example Artist - My Song (Official Audio)"),
        ],
        max_duration_seconds=600,
    )
    assert decision.selected is not None
    assert decision.selected.source_id == "bbbbbbbbbbb"


def test_source_selection_requires_review_when_candidates_are_ambiguous() -> None:
    decision = select_source(
        TrackIntent(artist="Example Artist", title="My Song", duration_seconds=180),
        [
            _candidate("aaaaaaaaaaa", "Example Artist - My Song"),
            _candidate("bbbbbbbbbbb", "Example Artist - My Song"),
        ],
        max_duration_seconds=600,
    )
    assert decision.needs_review
    assert decision.selected is None
    assert decision.ambiguous


def test_source_selection_excludes_overlong_media() -> None:
    decision = select_source(
        TrackIntent(artist="Example Artist", title="My Song"),
        [_candidate("aaaaaaaaaaa", "Example Artist - My Song", duration=3601)],
        max_duration_seconds=1800,
    )
    assert decision.needs_review
    assert not decision.ranked
    assert not decision.ambiguous


class _SelectionQueue:
    lease_seconds = 30

    def set_progress(self, *_args: object, **_kwargs: object) -> None:
        return None


class _SelectionMonitor:
    def raise_if_unusable(self) -> None:
        return None


def _processor(session_factory) -> DownloadJobProcessor:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.session_factory = session_factory
    processor.queue = _SelectionQueue()
    processor.settings = SimpleNamespace(max_agent_seconds=2)
    return processor


def _ambiguous_decision():
    return select_source(
        TrackIntent(artist="Example Artist", title="My Song", duration_seconds=180),
        [
            _candidate("aaaaaaaaaaa", "Example Artist - My Song"),
            _candidate("bbbbbbbbbbb", "Example Artist - My Song"),
        ],
        max_duration_seconds=600,
    )


def _lease(session_factory) -> JobLease:
    token = "fence"  # noqa: S105 - inert fencing fixture
    snapshot = {
        "artist": "Example Artist",
        "title": "My Song",
        "duration_seconds": 180,
        "version_signature": "studio",
    }
    with session_factory.begin() as session:
        user = User(
            username="source-selector",
            username_normalized="source-selector",
            password_hash="fixture-hash",  # noqa: S106 - inert fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="source selector")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add My Song by Example Artist",
            action="add",
            idempotency_key="source-selector",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Example Artist",
            title="My Song",
        )
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key="source-selector",
            status="active",
            stage="waiting_ai",
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    return JobLease(
        job_id=job_id,
        token=token,
        approved_snapshot=snapshot,
        retry_count=0,
    )


def _wait_for_task(session_factory) -> str:
    for _attempt in range(100):
        with session_factory() as session:
            task_id = session.scalar(
                select(ServiceTask.id).where(ServiceTask.kind == "select_source")
            )
            if task_id is not None:
                return task_id
        time.sleep(0.01)
    raise AssertionError("source-selection task was not enqueued")


@pytest.mark.parametrize(
    ("result", "expected", "error"),
    [
        (
            {
                "selected_source_id": "bbbbbbbbbbb",
                "needs_review": False,
                "rationale": "best match",
            },
            "https://www.youtube.com/watch?v=bbbbbbbbbbb",
            None,
        ),
        (
            {
                "selected_source_id": "unknown-id",
                "needs_review": False,
                "rationale": "invalid",
            },
            None,
            "unknown candidate ID",
        ),
    ],
)
def test_ambiguous_source_uses_finite_id_web_broker(
    session_factory, result: dict[str, object], expected: str | None, error: str | None
) -> None:
    processor = _processor(session_factory)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            processor._resolve_ambiguous_source,
            _lease(session_factory),
            _SelectionMonitor(),
            _ambiguous_decision(),
        )
        task_id = _wait_for_task(session_factory)
        with session_factory.begin() as session:
            task = session.get(ServiceTask, task_id)
            assert task is not None
            assert "watch?v=" not in task.payload_json
            task.state = "completed"
            task.result_json = json.dumps(result)
        if error is None:
            assert future.result(timeout=3) == expected
        else:
            with pytest.raises(SourceNeedsReview, match=error):
                future.result(timeout=3)


def test_failed_source_selector_falls_back_to_review(session_factory) -> None:
    processor = _processor(session_factory)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            processor._resolve_ambiguous_source,
            _lease(session_factory),
            _SelectionMonitor(),
            _ambiguous_decision(),
        )
        task_id = _wait_for_task(session_factory)
        with session_factory.begin() as session:
            task = session.get(ServiceTask, task_id)
            assert task is not None
            task.state = "failed"
            task.last_error = "provider unavailable"
        with pytest.raises(SourceNeedsReview, match="source selector failed"):
            future.result(timeout=3)


def test_ambiguous_source_wait_observes_worker_shutdown(session_factory) -> None:
    processor = _processor(session_factory)
    shutdown = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            processor._resolve_ambiguous_source,
            _lease(session_factory),
            _SelectionMonitor(),
            _ambiguous_decision(),
            shutdown,
        )
        _wait_for_task(session_factory)
        shutdown.set()
        with pytest.raises(DownloadCancelled, match="source selection was cancelled"):
            future.result(timeout=1)
