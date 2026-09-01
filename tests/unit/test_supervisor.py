from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Conversation, Request, ServiceTask, User
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.services.supervisor import (
    ClaimedTask,
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
