from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.events import _event_stream
from app.api.usage import usage_snapshot
from app.db.enums import JobStatus
from app.db.models import Conversation, DownloadJob, Event, OpenAICall, Request, RequestTrack, User
from app.repositories.events import EventRepository, make_event
from app.repositories.jobs import TERMINAL_STATUSES, JobRepository


def test_event_payload_bounds_large_integers_before_serialization(session_factory):
    with session_factory() as session:
        event = make_event(
            session,
            entity_type="operations",
            event_type="operation.diagnostic",
            message="Safe diagnostic",
            details={"value": 1 << 100_000},
        )
    assert json.loads(event.details_json) == {"value": "[out of range]"}


def seed_jobs(factory, count=12):
    with factory.begin() as session:
        owners = [
            User(
                username=f"listener{i}",
                username_normalized=f"listener{i}",
                password_hash="unused",  # noqa: S106
            )
            for i in range(2)
        ]
        session.add_all(owners)
        session.flush()
        ids = []
        for owner in owners:
            conversation = Conversation(user_id=owner.id, title="Private music")
            session.add(conversation)
            session.flush()
            request = Request(
                user_id=owner.id,
                conversation_id=conversation.id,
                raw_text="Private request",
                action="add",
                input_kind="natural_language",
                requested_count=1,
                status="queued",
                idempotency_key=owner.id,
            )
            session.add(request)
            session.flush()
            owner_jobs = []
            for ordinal in range(count):
                track = RequestTrack(
                    request_id=request.id, ordinal=ordinal, artist="Artist", title=f"Song{ordinal}"
                )
                session.add(track)
                session.flush()
                job = DownloadJob(
                    request_track_id=track.id,
                    approved_snapshot_json='{"artist":"Artist"}',
                    dedup_key=f"{owner.id}-{ordinal}",
                    status="completed",
                    stage="completed",
                    final_relative_path=f"Artist/Song{ordinal}.mp3",
                )
                session.add(job)
                session.flush()
                owner_jobs.append(job.id)
            ids.append(owner_jobs)
        return [owner.id for owner in owners], ids


@pytest.mark.parametrize("state", list(JobStatus))
def test_dismiss_is_explicit_terminal_allowlist(session_factory, state):
    owners, jobs = seed_jobs(session_factory, 1)
    repository = JobRepository(session_factory)
    with session_factory.begin() as session:
        session.get(DownloadJob, jobs[0][0]).status = state
    if state in TERMINAL_STATUSES:
        repository.mutate_for_user(jobs[0][0], owners[0], "dismiss")
        repository.mutate_for_user(jobs[0][0], owners[0], "dismiss")
        assert repository.page_for_user(owners[0]).total == 0
        assert repository.page_for_user(owners[0], view="hidden").total == 1
        with session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Event)) == 1
    else:
        with pytest.raises(ValueError, match="finished"):
            repository.mutate_for_user(jobs[0][0], owners[0], "dismiss")


def test_clear_atomic_race_preserves_related_history_and_music(session_factory, tmp_path):
    owners, jobs = seed_jobs(session_factory, 80)
    music = tmp_path / "owned.mp3"
    music.write_bytes(b"do not delete")
    repository = JobRepository(session_factory)
    with session_factory() as session:
        before = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (Request, RequestTrack, DownloadJob, Conversation)
        }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repository.clear_finished(owners[0]), range(2)))
    assert sorted(results) == [0, 80]
    assert repository.page_for_user(owners[1]).total == 80
    assert repository.page_for_user(owners[0], view="hidden", page=2, page_size=25).page == 2
    with session_factory() as session:
        assert {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (Request, RequestTrack, DownloadJob, Conversation)
        } == before
        events = list(session.scalars(select(Event)))
        assert len(events) == 1 and events[0].user_id == owners[0]
        assert json.loads(events[0].details_json) == {"count": 80}
    assert music.read_bytes() == b"do not delete"
    with pytest.raises(LookupError):
        repository.mutate_for_user(jobs[0][0], owners[1], "restore")
    repository.mutate_for_user(jobs[0][0], owners[0], "restore")
    repository.mutate_for_user(jobs[0][0], owners[0], "restore")
    assert repository.page_for_user(owners[0]).total == 1


def test_retry_hidden_failure_restores_and_queues_in_one_transaction(session_factory):
    owners, jobs = seed_jobs(session_factory, 1)
    with session_factory.begin() as session:
        session.get(DownloadJob, jobs[0][0]).status = "failed"
    repository = JobRepository(session_factory)
    repository.clear_finished(owners[0], ["failed"])
    restored = repository.mutate_for_user(jobs[0][0], owners[0], "retry")
    assert restored.dismissed_at is None and restored.status == "queued"
    for invalid in ([], ["completed", "completed"], ["active"]):
        with pytest.raises(ValueError):
            repository.clear_finished(owners[0], invalid)


def test_visible_queue_orders_active_before_new_terminal_rows(session_factory):
    owners, jobs = seed_jobs(session_factory, 60)
    with session_factory.begin() as session:
        session.get(DownloadJob, jobs[0][0]).status = "needs_review"
    page = JobRepository(session_factory).page_for_user(owners[0], page_size=25)
    assert page.jobs[0].id == jobs[0][0]
    assert page.pages == 3 and page.counts["attention"] == 1


def test_event_owner_resolution_and_shared_payload_allowlist(session_factory):
    owners, jobs = seed_jobs(session_factory, 1)
    with session_factory.begin() as session:
        session.add(
            make_event(
                session,
                entity_type="job",
                entity_id=jobs[0][0],
                event_type="job.failed",
                message="Private failure",
            )
        )
        session.add(
            make_event(
                session,
                entity_type="library",
                event_type="library.updated",
                message="secret path",
                audience="all_authenticated",
                details={"changed": 3, "path": "/srv/secret", "job_id": jobs[1][0]},
            )
        )
        session.add(
            make_event(
                session, entity_type="operations", event_type="ops.failed", message="Admin only"
            )
        )
    repository = EventRepository(session_factory)
    first = repository.visible_after(0, user_id=owners[0], is_admin=False, through=1000)
    second = repository.visible_after(0, user_id=owners[1], is_admin=False, through=1000)
    admin = repository.visible_after(0, user_id=owners[1], is_admin=True, through=1000)
    assert len(first) == 2 and len(second) == 1 and len(admin) == 2
    assert second[0].message == "Shared library updated"
    assert json.loads(second[0].details_json) == {"changed": 3}
    assert second[0].entity_id is None


@pytest.mark.asyncio
async def test_sse_filters_gaps_without_skipping_batch_and_rechecks_revocation(
    session_factory, monkeypatch
):
    owners, _jobs = seed_jobs(session_factory, 1)
    with session_factory.begin() as session:
        for index in range(205):
            session.add(
                make_event(
                    session,
                    entity_type="request",
                    event_type="request.updated",
                    message=str(index),
                    audience="user",
                    user_id=owners[index % 2],
                )
            )
    alive = True
    touches = []

    def resolve(_token, *, touch):
        touches.append(touch)
        return (
            SimpleNamespace(user_id=owners[0], role="user", must_change_password=False)
            if alive
            else None
        )

    async def disconnected():
        return False

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                events=EventRepository(session_factory),
                auth=SimpleNamespace(resolve_session=resolve),
            )
        ),
        cookies={},
        is_disconnected=disconnected,
    )
    stream = _event_stream(request, 0)
    messages = [await anext(stream) for _ in range(103)]
    assert all("event: update" in message for message in messages)
    assert '"message":"204"' in messages[-1]
    alive = False
    assert "signed_out" in await anext(stream)
    assert touches and not any(touches)
    await stream.aclose()


def test_usage_scopes_reconcile_and_unknown_is_not_zero(session_factory):
    owners, _ = seed_jobs(session_factory, 1)
    with session_factory.begin() as session:
        for owner, amount, reported in [
            (owners[0], 10, True),
            (owners[1], 20, True),
            (None, 0, False),
        ]:
            session.add(
                OpenAICall(
                    model="test",
                    prompt_version="v1",
                    prompt_hash="0" * 64,
                    owner_user_id=owner,
                    total_tokens=amount,
                    usage_reported=reported,
                    estimated_cost_microusd=amount if reported else None,
                    status="completed",
                    pricing_snapshot_json="{}",
                    created_at=datetime.now(UTC),
                )
            )
    first = usage_snapshot(session_factory, user_id=owners[0], scope="own")
    second = usage_snapshot(session_factory, user_id=owners[1], scope="own")
    system = usage_snapshot(session_factory, scope="system")
    all_usage = usage_snapshot(session_factory)
    assert first["daily"][0]["total_tokens"] == 10
    assert second["daily"][0]["total_tokens"] == 20
    assert system["daily"][0]["unknown_usage_calls"] == 1
    assert all_usage["daily"][0]["total_tokens"] == 30
    assert all_usage["daily"][0]["estimated_cost_microusd"] is None
    assert len(first["recent"]) == 1
