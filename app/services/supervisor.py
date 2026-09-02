from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import shutil
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import DateTime, Engine, bindparam, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Request, RequestTrack, ScanRun, ServiceHeartbeat, ServiceTask
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.services.confirmation import confirmation_decision

logger = logging.getLogger(__name__)
_DATETIME = DateTime(timezone=True)


class WebOrchestration(Protocol):
    def run_request(self, request_id: str) -> Awaitable[None] | None: ...

    def select_source(
        self, payload: dict[str, object]
    ) -> Awaitable[dict[str, object]] | dict[str, object]: ...

    def match_canonical(
        self, payload: dict[str, object]
    ) -> Awaitable[dict[str, object]] | dict[str, object]: ...


@dataclass(frozen=True)
class ClaimedTask:
    id: str
    kind: str
    payload: dict[str, object]
    lease_token: str
    attempts: int


class RequestLeaseBusy(RuntimeError):
    pass


class ConfirmationGatePending(RuntimeError):
    """An exact request is waiting on a recoverable local safety gate."""


class WebTaskSupervisor:
    def __init__(
        self,
        *,
        engine: Engine,
        factory: sessionmaker[Session],
        settings: Settings,
        orchestration: WebOrchestration | None,
        jobs: JobRepository,
        events: EventRepository,
    ) -> None:
        self.engine = engine
        self.factory = factory
        self.settings = settings
        self.orchestration = orchestration
        self.jobs = jobs
        self.events = events
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        heartbeat_at = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            if loop.time() >= heartbeat_at:
                self._heartbeat(0)
                heartbeat_at = loop.time() + 10
            claimed = self._claim()
            if claimed is None:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            self._heartbeat(1)
            lease_renewal = asyncio.create_task(
                self._renew_task_lease(claimed), name=f"web-task-lease-{claimed.id}"
            )
            try:
                async with asyncio.timeout(self.settings.max_agent_seconds):
                    result = await self._execute(claimed)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("web service task failed", extra={"request_id": claimed.id})
                self._fail(claimed, error)
            else:
                self._complete(claimed, result)
            finally:
                lease_renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await lease_renewal
                self._heartbeat(0)

    async def _execute(self, task: ClaimedTask) -> dict[str, object] | None:
        request_id = str(task.payload.get("request_id", ""))
        if task.kind == "orchestrate_request":
            if not request_id:
                raise ValueError("service task is missing request_id")
            if self.orchestration is None:
                raise RuntimeError("OpenAI orchestration is unavailable")
            method = self.orchestration.run_request
            result = method(request_id)
            if inspect.isawaitable(result):
                await result
            with self.factory() as session:
                request = session.get(Request, request_id)
                if request is not None and request.status == "orchestrating":
                    raise RequestLeaseBusy("request orchestration is still owned by another lease")
            self._apply_confirmation(request_id)
            return None
        if task.kind == "confirm_request":
            if not request_id:
                raise ValueError("service task is missing request_id")
            self._apply_confirmation(request_id)
            return None
        if task.kind == "select_source":
            if self.orchestration is None or not hasattr(self.orchestration, "select_source"):
                raise RuntimeError("AI source selection is unavailable")
            selector_result = self.orchestration.select_source(task.payload)
            if inspect.isawaitable(selector_result):
                selector_result = await selector_result
            if not isinstance(selector_result, dict):
                raise ValueError("source selector returned an invalid result")
            return selector_result
        if task.kind == "match_canonical":
            if self.orchestration is None or not hasattr(self.orchestration, "match_canonical"):
                raise RuntimeError("AI canonical matching is unavailable")
            match_result = self.orchestration.match_canonical(task.payload)
            if inspect.isawaitable(match_result):
                match_result = await match_result
            if not isinstance(match_result, dict):
                raise ValueError("canonical matcher returned an invalid result")
            return match_result
        raise ValueError(f"unsupported web service task kind: {task.kind}")

    def _claim(self) -> ClaimedTask | None:
        now = datetime.now(UTC)
        lease_expires = now + timedelta(seconds=self.settings.lease_seconds)
        token = secrets.token_hex(24)
        connection = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            row = (
                connection.execute(
                    text(
                        "SELECT id, kind, payload_json, attempts FROM service_tasks "
                        "WHERE target='web' AND available_at <= :now AND "
                        "(state IN ('queued','retry_wait') OR "
                        "(state='running' AND lease_expires_at < :now)) "
                        "ORDER BY created_at LIMIT 1"
                    ).bindparams(bindparam("now", type_=_DATETIME)),
                    {"now": now},
                )
                .mappings()
                .first()
            )
            if row is None:
                connection.exec_driver_sql("COMMIT")
                return None
            result = connection.execute(
                text(
                    "UPDATE service_tasks SET state='running', lease_token=:token, "
                    "lease_expires_at=:expiry, attempts=attempts+1, updated_at=:now "
                    "WHERE id=:id AND (state IN ('queued','retry_wait') OR lease_expires_at < :now)"
                ).bindparams(
                    bindparam("expiry", type_=_DATETIME),
                    bindparam("now", type_=_DATETIME),
                ),
                {"token": token, "expiry": lease_expires, "now": now, "id": row["id"]},
            )
            if result.rowcount != 1:
                connection.exec_driver_sql("ROLLBACK")
                return None
            connection.exec_driver_sql("COMMIT")
            return ClaimedTask(
                id=str(row["id"]),
                kind=str(row["kind"]),
                payload=json.loads(str(row["payload_json"])),
                lease_token=token,
                attempts=int(row["attempts"]) + 1,
            )
        except Exception:
            if connection.in_transaction():
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.close()

    def _complete(self, task: ClaimedTask, result_payload: dict[str, object] | None) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE service_tasks SET state='completed', result_json=:result, "
                    "lease_token=NULL, lease_expires_at=NULL, updated_at=:now "
                    "WHERE id=:id AND lease_token=:token"
                ).bindparams(bindparam("now", type_=_DATETIME)),
                {
                    "result": (
                        json.dumps(result_payload, ensure_ascii=False, separators=(",", ":"))
                        if result_payload is not None
                        else None
                    ),
                    "now": now,
                    "id": task.id,
                    "token": task.lease_token,
                },
            )
            if result.rowcount != 1:
                logger.warning(
                    "service task completion lost its lease", extra={"request_id": task.id}
                )

    def _fail(self, task: ClaimedTask, error: Exception) -> None:
        now = datetime.now(UTC)
        request_busy = isinstance(error, RequestLeaseBusy)
        confirmation_pending = isinstance(error, ConfirmationGatePending)
        terminal = not (request_busy or confirmation_pending) and (
            task.attempts >= 5 or isinstance(error, ValueError)
        )
        delay = (
            max(5, self.settings.lease_seconds // 2)
            if request_busy or confirmation_pending
            else min(300, 2 ** min(task.attempts, 8))
        )
        retry_kind = (
            "confirm_request"
            if confirmation_pending and task.kind == "orchestrate_request"
            else task.kind
        )
        with self.engine.begin() as connection:
            updated = connection.execute(
                text(
                    "UPDATE service_tasks SET state=:state, kind=:kind, available_at=:available, "
                    "lease_token=NULL, lease_expires_at=NULL, last_error=:error, updated_at=:now "
                    "WHERE id=:id AND lease_token=:token"
                ).bindparams(
                    bindparam("available", type_=_DATETIME),
                    bindparam("now", type_=_DATETIME),
                ),
                {
                    "state": "failed" if terminal else "retry_wait",
                    "kind": retry_kind,
                    "available": now + timedelta(seconds=delay),
                    "error": str(error)[:1000],
                    "now": now,
                    "id": task.id,
                    "token": task.lease_token,
                },
            )
        if updated.rowcount != 1:
            logger.warning("service task failure lost its lease", extra={"request_id": task.id})
            return
        request_id = str(task.payload.get("request_id", ""))
        if terminal and request_id and task.kind in {"orchestrate_request", "confirm_request"}:
            with self.factory.begin() as session:
                request = session.get(Request, request_id)
                if request:
                    request.status = "failed"
                    request.error_code = "orchestration_failed"
                    request.error_message = str(error)[:500]
            self.events.emit("request", "request.failed", "Request processing failed", request_id)

    async def _renew_task_lease(self, task: ClaimedTask) -> None:
        interval = max(5.0, min(30.0, self.settings.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            if not self._extend_task_lease(task):
                return

    def _extend_task_lease(self, task: ClaimedTask, *, now: datetime | None = None) -> bool:
        timestamp = now or datetime.now(UTC)
        with self.factory.begin() as session:
            result = session.execute(
                update(ServiceTask)
                .where(
                    ServiceTask.id == task.id,
                    ServiceTask.state == "running",
                    ServiceTask.lease_token == task.lease_token,
                )
                .values(
                    lease_expires_at=timestamp + timedelta(seconds=self.settings.lease_seconds),
                    updated_at=timestamp,
                )
            )
            return result.rowcount == 1

    def _apply_confirmation(self, request_id: str) -> None:
        with self.factory() as session:
            request = session.get(Request, request_id)
            if request is None or request.status in {"auto_queued", "queued"}:
                return
            tracks = list(
                session.scalars(
                    select(RequestTrack)
                    .where(RequestTrack.request_id == request_id)
                    .order_by(RequestTrack.ordinal)
                )
            )
            decision = confirmation_decision(request, tracks, self.settings)
            user_id = request.user_id
            scan_ready = (
                not self.settings.initial_scan_required
                or session.scalar(
                    select(ScanRun.id)
                    .where(ScanRun.kind == "initial", ScanRun.status == "completed")
                    .limit(1)
                )
                is not None
            )
        if not decision.auto_queue:
            return
        if not scan_ready:
            raise ConfirmationGatePending(
                "exact Add request is waiting for the initial library scan"
            )
        try:
            disk_ready = (
                shutil.disk_usage(self.settings.music_path).free >= self.settings.min_free_bytes
            )
        except OSError:
            disk_ready = False
        if not disk_ready:
            raise ConfirmationGatePending(
                "exact Add request is waiting for sufficient library disk space"
            )
        job_ids = self.jobs.queue_approved(request_id, user_id, [tracks[0].id])
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request:
                request.status = "auto_queued"
        self.events.emit(
            "request",
            "request.auto_queued",
            "Exact single-track Add request queued automatically",
            request_id,
            {"job_ids": job_ids},
        )

    def _heartbeat(self, active: int) -> None:
        now = datetime.now(UTC)
        with self.factory.begin() as session:
            heartbeat = session.get(ServiceHeartbeat, "web")
            if heartbeat is None:
                heartbeat = ServiceHeartbeat(
                    service="web", service_version=self.settings.app_version
                )
                session.add(heartbeat)
            heartbeat.service_version = self.settings.app_version
            heartbeat.last_heartbeat_at = now
            heartbeat.active_work_count = active
