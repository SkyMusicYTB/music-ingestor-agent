from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Conversation,
    DownloadJob,
    Request,
    RequestTrack,
    ScanRun,
    ServiceTask,
    User,
)
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.services.supervisor import (
    ClaimedTask,
    ConfirmationGatePending,
    RequestLeaseBusy,
    WebTaskSupervisor,
)


def _rows(
    factory: sessionmaker[Session], *, kind: str, request_status: str
) -> tuple[str, str, str]:
    token = "lease-token"  # noqa: S105 - inert fencing-token fixture
    with factory.begin() as session:
        user = User(
            username="listener",
            username_normalized="listener-supervisor",
            password_hash="fixture-hash",  # noqa: S106 - inert fixture value
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Supervisor")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="one track",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status=request_status,
            idempotency_key=f"supervisor-{kind}",
        )
        session.add(request)
        session.flush()
        task = ServiceTask(
            target="web",
            kind=kind,
            payload_json=json.dumps({"request_id": request.id}),
            state="running",
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            attempts=5,
        )
        session.add(task)
        session.flush()
        return request.id, task.id, token


def _supervisor(engine: Engine, factory: sessionmaker[Session], settings) -> WebTaskSupervisor:
    return WebTaskSupervisor(
        engine=engine,
        factory=factory,
        settings=settings,
        orchestration=None,
        jobs=JobRepository(factory),
        events=EventRepository(factory),
    )


def _exact_confirmation_rows(
    factory: sessionmaker[Session], *, suffix: str, task_kind: str
) -> tuple[str, str, str]:
    token = f"confirmation-lease-{suffix}"
    recording_mbid = "11111111-1111-1111-1111-111111111111"
    with factory.begin() as session:
        user = User(
            username=f"listener-{suffix}",
            username_normalized=f"listener-confirmation-{suffix}",
            password_hash="fixture-hash",  # noqa: S106 - inert fixture value
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Exact confirmation")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Yellow by Coldplay",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="preview",
            idempotency_key=f"confirmation-{suffix}",
        )
        session.add(request)
        session.flush()
        session.add(
            RequestTrack(
                request_id=request.id,
                ordinal=1,
                artist="Coldplay",
                title="Yellow",
                recording_mbid=recording_mbid,
                canonical_identity_verified=True,
                metadata_confidence=0.96,
                metadata_provenance_json=json.dumps(
                    {
                        "automatic_association": True,
                        "source": "musicbrainz_search_recordings",
                        "recording_mbid": recording_mbid,
                        "score": 96,
                    }
                ),
                selected=True,
            )
        )
        task = ServiceTask(
            target="web",
            kind=task_kind,
            payload_json=json.dumps({"request_id": request.id}, separators=(",", ":")),
            state="running",
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            attempts=1,
        )
        session.add(task)
        session.flush()
        return request.id, task.id, token


def test_source_selector_failure_does_not_fail_parent_request(
    engine: Engine, session_factory: sessionmaker[Session], settings
) -> None:
    request_id, task_id, token = _rows(
        session_factory, kind="select_source", request_status="queued"
    )
    supervisor = _supervisor(engine, session_factory, settings)

    supervisor._fail(
        ClaimedTask(task_id, "select_source", {"request_id": request_id}, token, 5),
        RuntimeError("selector unavailable"),
    )

    with session_factory() as session:
        assert session.get(ServiceTask, task_id).state == "failed"  # type: ignore[union-attr]
        assert session.get(Request, request_id).status == "queued"  # type: ignore[union-attr]


def test_busy_request_lease_is_retried_and_task_lease_is_fenced(
    engine: Engine, session_factory: sessionmaker[Session], settings
) -> None:
    request_id, task_id, token = _rows(
        session_factory, kind="orchestrate_request", request_status="orchestrating"
    )
    supervisor = _supervisor(engine, session_factory, settings)
    task = ClaimedTask(task_id, "orchestrate_request", {"request_id": request_id}, token, 9)

    before = datetime.now(UTC)
    assert supervisor._extend_task_lease(task, now=before)
    assert not supervisor._extend_task_lease(
        ClaimedTask(task_id, task.kind, task.payload, "wrong-token", task.attempts),
        now=before,
    )
    supervisor._fail(task, RequestLeaseBusy("still owned"))

    with session_factory() as session:
        stored_task = session.get(ServiceTask, task_id)
        stored_request = session.get(Request, request_id)
        assert stored_task is not None and stored_task.state == "retry_wait"
        assert stored_task.available_at > before.replace(tzinfo=None)
        assert stored_request is not None and stored_request.status == "orchestrating"


def test_stale_task_failure_cannot_mutate_parent_request(
    engine: Engine, session_factory: sessionmaker[Session], settings
) -> None:
    request_id, task_id, _token = _rows(
        session_factory, kind="orchestrate_request", request_status="preview"
    )
    supervisor = _supervisor(engine, session_factory, settings)

    supervisor._fail(
        ClaimedTask(
            task_id,
            "orchestrate_request",
            {"request_id": request_id},
            "stale-token",
            5,
        ),
        RuntimeError("stale owner failed"),
    )

    with session_factory() as session:
        stored_task = session.get(ServiceTask, task_id)
        stored_request = session.get(Request, request_id)
        assert stored_task is not None and stored_task.state == "running"
        assert stored_request is not None and stored_request.status == "preview"


@pytest.mark.asyncio
async def test_supervisor_dispatches_finite_canonical_match(
    engine: Engine, session_factory: sessionmaker[Session], settings
) -> None:
    class CanonicalMatcher:
        async def match_canonical(self, payload: dict[str, object]) -> dict[str, object]:
            assert payload["schema_version"] == 2
            return {"decision": {"decision": "ambiguous"}, "openai_call_id": "call_1"}

    supervisor = WebTaskSupervisor(
        engine=engine,
        factory=session_factory,
        settings=settings,
        orchestration=CanonicalMatcher(),  # type: ignore[arg-type]
        jobs=JobRepository(session_factory),
        events=EventRepository(session_factory),
    )

    result = await supervisor._execute(
        ClaimedTask(
            "task_1",
            "match_canonical",
            {"schema_version": 2, "job_id": "job_1"},
            "lease-token",
            1,
        )
    )

    assert result == {
        "decision": {"decision": "ambiguous"},
        "openai_call_id": "call_1",
    }


@pytest.mark.asyncio
async def test_initial_scan_gate_converts_completed_orchestration_to_durable_confirmation(
    engine: Engine, session_factory: sessionmaker[Session], settings
) -> None:
    request_id, task_id, token = _exact_confirmation_rows(
        session_factory, suffix="scan-gate", task_kind="orchestrate_request"
    )
    runtime_settings = settings.model_copy(
        update={"initial_scan_required": True, "min_free_bytes": 104_857_600}
    )

    class NoOpOrchestration:
        calls = 0

        async def run_request(self, received_request_id: str) -> None:
            assert received_request_id == request_id
            self.calls += 1

    orchestration = NoOpOrchestration()
    supervisor = WebTaskSupervisor(
        engine=engine,
        factory=session_factory,
        settings=runtime_settings,
        orchestration=orchestration,  # type: ignore[arg-type]
        jobs=JobRepository(session_factory),
        events=EventRepository(session_factory),
    )
    original = ClaimedTask(task_id, "orchestrate_request", {"request_id": request_id}, token, 1)
    with pytest.raises(ConfirmationGatePending, match="initial library scan") as blocked:
        await supervisor._execute(original)
    supervisor._fail(original, blocked.value)

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        request = session.get(Request, request_id)
        assert task is not None and task.state == "retry_wait"
        assert task.kind == "confirm_request"
        assert request is not None and request.status == "preview"
        assert session.scalar(select(func.count(DownloadJob.id))) == 0
    assert orchestration.calls == 1

    recovery_token = "confirmation-recovery-scan"  # noqa: S105 - inert fixture token
    with session_factory.begin() as session:
        session.add(ScanRun(kind="initial", generation=1, status="completed"))
        task = session.get(ServiceTask, task_id)
        assert task is not None
        task.state = "running"
        task.lease_token = recovery_token
        task.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    recovery = ClaimedTask(
        task_id, "confirm_request", {"request_id": request_id}, recovery_token, 2
    )
    assert await supervisor._execute(recovery) is None
    supervisor._complete(recovery, None)

    with session_factory() as session:
        assert session.get(Request, request_id).status == "auto_queued"  # type: ignore[union-attr]
        assert session.get(ServiceTask, task_id).state == "completed"  # type: ignore[union-attr]
        assert session.scalar(select(func.count(DownloadJob.id))) == 1
    assert orchestration.calls == 1
    supervisor._apply_confirmation(request_id)
    with session_factory() as session:
        assert session.scalar(select(func.count(DownloadJob.id))) == 1


@pytest.mark.asyncio
async def test_low_disk_confirmation_retries_and_autoqueues_once_after_recovery(
    engine: Engine, session_factory: sessionmaker[Session], settings, monkeypatch
) -> None:
    request_id, task_id, token = _exact_confirmation_rows(
        session_factory, suffix="disk-gate", task_kind="confirm_request"
    )
    runtime_settings = settings.model_copy(
        update={"initial_scan_required": False, "min_free_bytes": 104_857_600}
    )
    supervisor = _supervisor(engine, session_factory, runtime_settings)
    task = ClaimedTask(task_id, "confirm_request", {"request_id": request_id}, token, 8)
    monkeypatch.setattr(
        "app.services.supervisor.shutil.disk_usage", lambda _path: SimpleNamespace(free=0)
    )

    with pytest.raises(ConfirmationGatePending, match="disk space") as blocked:
        await supervisor._execute(task)
    supervisor._fail(task, blocked.value)
    with session_factory() as session:
        stored = session.get(ServiceTask, task_id)
        assert stored is not None and stored.state == "retry_wait"
        assert stored.kind == "confirm_request"
        assert session.get(Request, request_id).status == "preview"  # type: ignore[union-attr]

    recovery_token = "confirmation-recovery-disk"  # noqa: S105 - inert fixture token
    with session_factory.begin() as session:
        stored = session.get(ServiceTask, task_id)
        assert stored is not None
        stored.state = "running"
        stored.lease_token = recovery_token
        stored.lease_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    monkeypatch.setattr(
        "app.services.supervisor.shutil.disk_usage",
        lambda _path: SimpleNamespace(free=runtime_settings.min_free_bytes),
    )
    recovery = ClaimedTask(
        task_id, "confirm_request", {"request_id": request_id}, recovery_token, 9
    )
    assert await supervisor._execute(recovery) is None
    supervisor._complete(recovery, None)

    with session_factory() as session:
        assert session.get(Request, request_id).status == "auto_queued"  # type: ignore[union-attr]
        assert session.get(ServiceTask, task_id).state == "completed"  # type: ignore[union-attr]
        assert session.scalar(select(func.count(DownloadJob.id))) == 1
