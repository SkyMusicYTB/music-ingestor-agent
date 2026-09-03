from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import JobStage, JobStatus, TaskState, TaskTarget
from app.db.models import DownloadJob, JobArtifact, JobDecision, Request, ServiceTask, Track
from app.repositories.decisions import candidate_set_fingerprint, create_pending_decision
from app.repositories.events import make_event
from app.services.filesystem import sha256_file, validate_relative_path


class LeaseLostError(RuntimeError):
    """The worker no longer owns the fencing token for a job."""


class JobCancellationRequested(RuntimeError):
    pass


def _decision_category(kind: str) -> str:
    normalized = kind.strip().casefold()
    return {
        "source": "acquisition_source",
        "acquisition_source": "acquisition_source",
        "metadata": "canonical_metadata",
        "canonical_metadata": "canonical_metadata",
        "duplicate": "possible_duplicate",
        "possible_duplicate": "possible_duplicate",
        "version": "recording_version",
        "recording_version": "recording_version",
    }.get(normalized, "acquisition_source")


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    token: str
    approved_snapshot: dict[str, Any]
    retry_count: int


@dataclass(frozen=True, slots=True)
class ServiceTaskLease:
    task_id: str
    token: str
    kind: str
    payload_version: int
    payload: dict[str, Any]
    attempts: int


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jittered_backoff(base_seconds: int, exponent: int, maximum_seconds: int) -> int:
    """Return capped exponential backoff with bounded cryptographic jitter."""

    nominal: int = int(min(maximum_seconds, base_seconds * (2 ** max(0, exponent))))
    spread: int = int(max(1, nominal // 5))
    jitter = int(secrets.randbelow(2 * spread + 1))
    return int(max(1, min(maximum_seconds, nominal - spread + jitter)))


def _decode_object(payload: str, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _job_event(
    session: Session,
    job_id: str,
    event_type: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        make_event(
            session,
            entity_type="job",
            entity_id=job_id,
            event_type=event_type,
            message=message[:500],
            details_json=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
        )
    )


def _without_job_warning(serialized: str, code: str) -> str:
    try:
        decoded = json.loads(serialized or "[]")
    except json.JSONDecodeError:
        decoded = []
    warnings = decoded if isinstance(decoded, list) else []
    retained = [
        item for item in warnings if not (isinstance(item, dict) and item.get("code") == code)
    ]
    return json.dumps(retained[-20:], ensure_ascii=False, separators=(",", ":"))


class DownloadJobQueue:
    def __init__(self, session_factory: sessionmaker[Session], *, lease_seconds: int = 120) -> None:
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds

    def claim_next(self, *, now: datetime | None = None) -> JobLease | None:
        timestamp = now or utc_now()
        expires = timestamp + timedelta(seconds=self.lease_seconds)
        token = secrets.token_hex(32)
        eligible = (
            DownloadJob.status.in_(
                [
                    JobStatus.QUEUED.value,
                    JobStatus.RETRY_WAIT.value,
                    JobStatus.WAITING_FOR_SPACE.value,
                ]
            )
            & (DownloadJob.available_at <= timestamp)
            & or_(DownloadJob.lease_token.is_(None), DownloadJob.lease_expires_at < timestamp)
        )
        candidate = (
            select(DownloadJob.id)
            .where(eligible)
            .order_by(
                DownloadJob.priority.asc(), DownloadJob.created_at.asc(), DownloadJob.id.asc()
            )
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(DownloadJob)
            .where(DownloadJob.id == candidate, eligible)
            .values(
                status=JobStatus.ACTIVE.value,
                stage=JobStage.RESOLVING_SOURCE.value,
                lease_token=token,
                lease_expires_at=expires,
                error_code=case(
                    (DownloadJob.error_code == "initial_scan_pending", DownloadJob.error_code),
                    else_=None,
                ),
                error_message=case(
                    (DownloadJob.error_code == "initial_scan_pending", DownloadJob.error_message),
                    else_=None,
                ),
                updated_at=timestamp,
            )
            .returning(
                DownloadJob.id,
                DownloadJob.lease_token,
                DownloadJob.approved_snapshot_json,
                DownloadJob.retry_count,
                DownloadJob.error_code,
            )
        )
        with self.session_factory() as session:
            row = session.execute(statement).mappings().one_or_none()
            if row is not None and row["error_code"] != "initial_scan_pending":
                _job_event(
                    session,
                    str(row["id"]),
                    "job.active",
                    "Download worker claimed the job",
                    details={"stage": JobStage.RESOLVING_SOURCE.value, "progress": 0.0},
                )
            session.commit()
        if row is None:
            return None
        return JobLease(
            job_id=str(row["id"]),
            token=str(row["lease_token"]),
            approved_snapshot=_decode_object(
                str(row["approved_snapshot_json"]), label="approved job snapshot"
            ),
            retry_count=int(row["retry_count"]),
        )

    def heartbeat(self, lease: JobLease, *, now: datetime | None = None) -> None:
        timestamp = now or utc_now()
        with self.session_factory() as session:
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
                .values(
                    lease_expires_at=timestamp + timedelta(seconds=self.lease_seconds),
                    updated_at=timestamp,
                )
            )
            if result.rowcount == 1:
                session.commit()
                return
            status = session.execute(
                select(DownloadJob.status).where(
                    DownloadJob.id == lease.job_id, DownloadJob.lease_token == lease.token
                )
            ).scalar_one_or_none()
            session.rollback()
        if status == JobStatus.CANCEL_REQUESTED.value:
            raise JobCancellationRequested("job cancellation was requested")
        raise LeaseLostError("job lease was lost while heartbeating")

    def set_progress(
        self,
        lease: JobLease,
        *,
        stage: JobStage | str,
        progress: float,
        now: datetime | None = None,
    ) -> None:
        if isinstance(progress, bool) or not 0.0 <= progress <= 1.0:
            raise ValueError("progress must be between 0 and 1")
        stage_value = stage.value if isinstance(stage, JobStage) else stage
        if stage_value not in {item.value for item in JobStage}:
            raise ValueError("unknown worker stage")
        timestamp = now or utc_now()
        with self.session_factory() as session:
            previous = session.execute(
                select(DownloadJob.stage, DownloadJob.progress).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
            ).one_or_none()
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
                .values(stage=stage_value, progress=progress, updated_at=timestamp)
            )
            if result.rowcount == 1:
                assert previous is not None
                old_stage, old_progress = str(previous[0]), float(previous[1])
                if old_stage != stage_value or progress >= old_progress + 0.05:
                    _job_event(
                        session,
                        lease.job_id,
                        "job.progress",
                        "Download job progressed",
                        details={"stage": stage_value, "progress": round(progress, 4)},
                    )
                session.commit()
                return
            status = session.execute(
                select(DownloadJob.status).where(
                    DownloadJob.id == lease.job_id, DownloadJob.lease_token == lease.token
                )
            ).scalar_one_or_none()
            session.rollback()
        if status == JobStatus.CANCEL_REQUESTED.value:
            raise JobCancellationRequested("job cancellation was requested")
        raise LeaseLostError("job lease was lost while updating progress")

    def complete(
        self,
        lease: JobLease,
        *,
        final_relative_path: str,
        final_sha256: str,
        final_track_id: str | None = None,
        now: datetime | None = None,
        published: bool = False,
    ) -> None:
        if len(final_sha256) != 64:
            raise ValueError("final_sha256 must be a SHA-256 hex digest")
        timestamp = now or utc_now()
        with self.session_factory() as session:
            eligible_statuses = [JobStatus.ACTIVE.value]
            if published:
                # Publication is the irreversible boundary. A cancellation that
                # races after no-clobber finalization must reconcile as complete,
                # never claim that an already-visible file was cancelled.
                eligible_statuses.append(JobStatus.CANCEL_REQUESTED.value)
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status.in_(eligible_statuses),
                )
                .values(
                    status=JobStatus.COMPLETED.value,
                    stage=JobStage.COMPLETED.value,
                    progress=1.0,
                    lease_token=None,
                    lease_expires_at=None,
                    final_track_id=final_track_id,
                    final_relative_path=final_relative_path,
                    final_sha256=final_sha256,
                    completed_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                status = session.execute(
                    select(DownloadJob.status).where(
                        DownloadJob.id == lease.job_id,
                        DownloadJob.lease_token == lease.token,
                    )
                ).scalar_one_or_none()
                session.rollback()
                if status == JobStatus.CANCEL_REQUESTED.value:
                    raise JobCancellationRequested("job cancellation was requested")
                raise LeaseLostError("job lease was lost before completion")
            _job_event(
                session,
                lease.job_id,
                "job.completed",
                "Download job completed",
                details={"stage": JobStage.COMPLETED.value, "progress": 1.0},
            )
            session.commit()

    def require_review(
        self,
        lease: JobLease,
        *,
        reason: str,
        options: list[dict[str, Any]] | None = None,
        category: str | None = None,
        max_rounds_per_category: int = 3,
        max_rounds_per_job: int = 8,
        now: datetime | None = None,
    ) -> bool:
        timestamp = now or utc_now()
        review_category = _decision_category(category) if category else None
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(DownloadJob).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
            )
            if job is None:
                session.rollback()
                raise LeaseLostError("job lease was lost while requesting review")
            grouped: dict[str, list[dict[str, Any]]] = {}
            for option in (options or [])[:24]:
                option_category = _decision_category(
                    str(option.get("kind") or review_category or "source")
                )
                grouped.setdefault(option_category, []).append(option)
            if not grouped:
                # Empty choices cannot be answered. Do not persist an empty
                # fingerprint that will be replayed forever on every Retry.
                metadata_failure = (
                    review_category == "canonical_metadata"
                    or job.stage == JobStage.RESOLVING_METADATA.value
                )
                job.status = JobStatus.FAILED.value
                if metadata_failure:
                    job.stage = JobStage.RESOLVING_METADATA.value
                job.error_code = "review_has_no_options"
                job.error_message = reason[:1000]
                job.lease_token = None
                job.lease_expires_at = None
                job.completed_at = timestamp
                job.updated_at = timestamp
                job.warnings_json = _without_job_warning(job.warnings_json, "needs_review")
                _job_event(
                    session,
                    job.id,
                    "job.failed",
                    "Metadata could not be confirmed"
                    if metadata_failure
                    else "No actionable safe acquisition option was found",
                    details={"code": "review_has_no_options"},
                )
                session.commit()
                return False
            pending_decisions: list[JobDecision] = []
            for category, category_options in grouped.items():
                fingerprint = candidate_set_fingerprint(category, category_options)
                existing = session.scalar(
                    select(JobDecision)
                    .where(
                        JobDecision.job_id == job.id,
                        JobDecision.category == category,
                        JobDecision.candidate_set_fingerprint == fingerprint,
                    )
                    .order_by(JobDecision.revision.desc())
                    .limit(1)
                )
                if existing is not None:
                    if existing.state == "pending":
                        pending_decisions.append(existing)
                    # A selected fingerprint is authoritative and must be replayed
                    # by the pipeline instead of being presented a second time.
                    continue
                if job.review_round_count >= max_rounds_per_job:
                    raise RuntimeError("exceptional review budget was exhausted")
                category_rounds = (
                    session.scalar(
                        select(func.count(JobDecision.id)).where(
                            JobDecision.job_id == job.id,
                            JobDecision.category == category,
                        )
                    )
                    or 0
                )
                if category_rounds >= max_rounds_per_category:
                    raise RuntimeError(f"{category} review budget was exhausted")
                pending_decisions.append(
                    create_pending_decision(
                        session,
                        job,
                        category=category,
                        reason=reason,
                        options=category_options,
                    )
                )
            if not pending_decisions:
                job.status = JobStatus.QUEUED.value
                job.stage = JobStage.QUEUED.value
                job.available_at = timestamp
                job.lease_token = None
                job.lease_expires_at = None
                job.error_code = None
                job.error_message = None
                job.updated_at = timestamp
                _job_event(
                    session,
                    lease.job_id,
                    "job.decision_replayed",
                    "A previous review decision was reused",
                )
                session.commit()
                return False
            job.status = JobStatus.NEEDS_REVIEW.value
            job.stage = (
                JobStage.RESOLVING_SOURCE.value
                if "acquisition_source" in grouped
                else JobStage.RESOLVING_METADATA.value
            )
            job.lease_token = None
            job.lease_expires_at = None
            # The review reason is already the error/message. Keep unrelated
            # warnings, but never duplicate that reason as another warning.
            job.warnings_json = _without_job_warning(job.warnings_json, "needs_review")
            job.error_code = "exceptional_review_required"
            job.error_message = reason[:1000]
            job.updated_at = timestamp
            _job_event(
                session,
                lease.job_id,
                "job.needs_review",
                "Download job needs review",
                details={"option_count": min(len(options or []), 10)},
            )
            session.commit()
            return True

    def add_warning(self, lease: JobLease, *, code: str, message: str) -> None:
        """Append one bounded visible warning while preserving lease fencing."""

        safe_code = code.strip()[:80]
        safe_message = message.strip()[:500]
        if not safe_code or not safe_message:
            raise ValueError("warning code and message are required")
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(DownloadJob).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
            )
            if job is None:
                raise LeaseLostError("job lease was lost while recording a warning")
            try:
                decoded = json.loads(job.warnings_json or "[]")
            except json.JSONDecodeError:
                decoded = []
            warnings = decoded if isinstance(decoded, list) else []
            if not any(
                isinstance(item, dict) and item.get("code") == safe_code for item in warnings
            ):
                warnings.append({"code": safe_code, "message": safe_message})
            job.warnings_json = json.dumps(
                warnings[-20:], ensure_ascii=False, separators=(",", ":")
            )
            _job_event(
                session,
                lease.job_id,
                "job.warning",
                safe_message,
                details={"code": safe_code},
            )

    def request_cancel(self, job_id: str, *, now: datetime | None = None) -> str | None:
        timestamp = now or utc_now()
        cancellable = [
            JobStatus.QUEUED.value,
            JobStatus.ACTIVE.value,
            JobStatus.RETRY_WAIT.value,
            JobStatus.NEEDS_REVIEW.value,
            JobStatus.WAITING_FOR_SPACE.value,
            JobStatus.CANCEL_REQUESTED.value,
        ]
        with self.session_factory() as session:
            row = session.execute(
                update(DownloadJob)
                .where(DownloadJob.id == job_id, DownloadJob.status.in_(cancellable))
                .values(
                    status=case(
                        (
                            DownloadJob.status.in_(
                                [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                            ),
                            JobStatus.CANCEL_REQUESTED.value,
                        ),
                        else_=JobStatus.CANCELLED.value,
                    ),
                    cancel_requested_at=timestamp,
                    lease_token=case(
                        (
                            DownloadJob.status.in_(
                                [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                            ),
                            DownloadJob.lease_token,
                        ),
                        else_=None,
                    ),
                    lease_expires_at=case(
                        (
                            DownloadJob.status.in_(
                                [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                            ),
                            DownloadJob.lease_expires_at,
                        ),
                        else_=None,
                    ),
                    completed_at=case(
                        (
                            DownloadJob.status.in_(
                                [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                            ),
                            DownloadJob.completed_at,
                        ),
                        else_=timestamp,
                    ),
                    updated_at=timestamp,
                )
                .returning(DownloadJob.status)
            ).scalar_one_or_none()
            if row is not None:
                state = str(row)
                _job_event(
                    session,
                    job_id,
                    "job.cancelled"
                    if state == JobStatus.CANCELLED.value
                    else "job.cancel_requested",
                    "Download job cancelled"
                    if state == JobStatus.CANCELLED.value
                    else "Download cancellation requested",
                )
            session.commit()
        return str(row) if row is not None else None

    def cancellation_requested(self, lease: JobLease) -> bool:
        with self.session_factory() as session:
            status = session.execute(
                select(DownloadJob.status).where(
                    DownloadJob.id == lease.job_id, DownloadJob.lease_token == lease.token
                )
            ).scalar_one_or_none()
        if status is None:
            raise LeaseLostError("job lease was lost while checking cancellation")
        return status == JobStatus.CANCEL_REQUESTED.value

    def acknowledge_cancel(self, lease: JobLease, *, now: datetime | None = None) -> None:
        timestamp = now or utc_now()
        with self.session_factory() as session:
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.CANCEL_REQUESTED.value,
                )
                .values(
                    status=JobStatus.CANCELLED.value,
                    lease_token=None,
                    lease_expires_at=None,
                    completed_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise LeaseLostError("job lease was lost while acknowledging cancellation")
            _job_event(
                session,
                lease.job_id,
                "job.cancelled",
                "Download job cancelled",
            )
            session.commit()

    def release_for_shutdown(self, lease: JobLease, *, now: datetime | None = None) -> str:
        """Release planned-shutdown work immediately without consuming retry budget."""

        timestamp = now or utc_now()
        with self.session_factory.begin() as session:
            job = session.scalar(
                select(DownloadJob).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status.in_(
                        [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                    ),
                )
            )
            if job is None:
                raise LeaseLostError("job lease was lost during worker shutdown")
            if job.status == JobStatus.CANCEL_REQUESTED.value:
                job.status = JobStatus.CANCELLED.value
                job.stage = JobStatus.CANCELLED.value
                job.completed_at = timestamp
                message = "Download cancellation acknowledged during worker shutdown"
            else:
                job.status = JobStatus.RETRY_WAIT.value
                job.available_at = timestamp
                job.error_code = "worker_shutdown"
                job.error_message = "worker stopped cleanly; resumable work was released"
                message = "Download released for restart"
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = timestamp
            _job_event(
                session,
                job.id,
                f"job.{job.status}",
                message,
                details={"stage": job.stage},
            )
            return job.status

    def fail(
        self,
        lease: JobLease,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        max_retries: int = 3,
        base_delay_seconds: int = 15,
        max_delay_seconds: int = 900,
        now: datetime | None = None,
    ) -> str:
        timestamp = now or utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(DownloadJob).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status.in_(
                        [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                    ),
                )
            ).scalar_one_or_none()
            if job is None:
                session.rollback()
                raise LeaseLostError("job lease was lost while recording failure")
            if job.status == JobStatus.CANCEL_REQUESTED.value:
                job.status = JobStatus.CANCELLED.value
                job.completed_at = timestamp
            elif retryable and job.retry_count < max_retries:
                delay = _jittered_backoff(
                    base_delay_seconds,
                    job.retry_count,
                    max_delay_seconds,
                )
                job.status = JobStatus.RETRY_WAIT.value
                job.retry_count += 1
                job.available_at = timestamp + timedelta(seconds=delay)
            else:
                job.status = JobStatus.FAILED.value
                job.completed_at = timestamp
            job.error_code = error_code[:80]
            job.error_message = error_message[:1000]
            job.lease_token = None
            job.lease_expires_at = None
            job.updated_at = timestamp
            result_status = job.status
            _job_event(
                session,
                lease.job_id,
                f"job.{result_status}",
                "Download job will be retried"
                if result_status == JobStatus.RETRY_WAIT.value
                else "Download job cancelled"
                if result_status == JobStatus.CANCELLED.value
                else "Download job failed",
                details={"error_code": error_code[:80]},
            )
            session.commit()
        return result_status

    def clear_library_wait(self, lease: JobLease) -> None:
        with self.session_factory.begin() as session:
            session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.error_code == "initial_scan_pending",
                )
                .values(error_code=None, error_message=None)
            )

    def defer_for_library_scan(self, lease: JobLease) -> str:
        """Keep work queued until the shared initial index covers the library."""
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            job = session.scalar(
                select(DownloadJob).where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status.in_(["active", "cancel_requested"]),
                )
            )
            if job is None:
                raise LeaseLostError("download lease lost while waiting for library scan")
            already_waiting = job.error_code == "initial_scan_pending"
            if job.status == "cancel_requested":
                job.status = "cancelled"
                job.completed_at = utc_now()
                _job_event(session, job.id, "job.cancelled", "Download cancelled")
            else:
                job.status = "queued"
                job.stage = "queued"
                job.available_at = utc_now() + timedelta(seconds=30)
                job.error_code = "initial_scan_pending"
                job.error_message = "Waiting for the initial library scan to complete"
                if not already_waiting:
                    _job_event(
                        session,
                        job.id,
                        "job.waiting_library",
                        "Download is waiting for the initial library scan",
                    )
            job.lease_token = None
            job.lease_expires_at = None
            result = job.status
            session.commit()
            return result

    def wait_for_space(
        self,
        lease: JobLease,
        *,
        retry_after_seconds: int = 60,
        now: datetime | None = None,
    ) -> None:
        timestamp = now or utc_now()
        with self.session_factory() as session:
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
                .values(
                    status=JobStatus.WAITING_FOR_SPACE.value,
                    available_at=timestamp + timedelta(seconds=retry_after_seconds),
                    lease_token=None,
                    lease_expires_at=None,
                    error_code="insufficient_space",
                    error_message="waiting for the configured free-space reserve",
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise LeaseLostError("job lease was lost while waiting for space")
            _job_event(
                session,
                lease.job_id,
                "job.waiting_space",
                "Download job is waiting for free space",
            )
            session.commit()

    def recover_expired(
        self,
        *,
        max_retries: int = 3,
        base_delay_seconds: int = 15,
        now: datetime | None = None,
    ) -> int:
        timestamp = now or utc_now()
        recovered = 0
        with self.session_factory() as session:
            jobs = session.execute(
                select(DownloadJob).where(
                    DownloadJob.status.in_(
                        [JobStatus.ACTIVE.value, JobStatus.CANCEL_REQUESTED.value]
                    ),
                    or_(
                        DownloadJob.lease_token.is_(None),
                        DownloadJob.lease_expires_at < timestamp,
                    ),
                )
            ).scalars()
            for job in jobs:
                if job.status == JobStatus.CANCEL_REQUESTED.value:
                    job.status = JobStatus.CANCELLED.value
                    job.completed_at = timestamp
                elif job.retry_count < max_retries:
                    delay = _jittered_backoff(base_delay_seconds, job.retry_count, 900)
                    job.status = JobStatus.RETRY_WAIT.value
                    job.retry_count += 1
                    job.available_at = timestamp + timedelta(seconds=delay)
                    job.error_code = "lease_expired"
                    job.error_message = "worker lease expired; the job will be retried"
                else:
                    job.status = JobStatus.FAILED.value
                    job.completed_at = timestamp
                    job.error_code = "lease_expired"
                    job.error_message = "worker lease expired and retry budget was exhausted"
                job.lease_token = None
                job.lease_expires_at = None
                job.updated_at = timestamp
                _job_event(
                    session,
                    job.id,
                    f"job.{job.status}",
                    "Expired worker lease recovered",
                    details={"error_code": job.error_code},
                )
                recovered += 1
            session.commit()
        return recovered

    def staging_job_ids_to_preserve(self) -> set[str]:
        with self.session_factory() as session:
            return set(
                session.scalars(
                    select(DownloadJob.id).where(
                        or_(
                            DownloadJob.status.in_(
                                [
                                    JobStatus.QUEUED.value,
                                    JobStatus.ACTIVE.value,
                                    JobStatus.RETRY_WAIT.value,
                                    JobStatus.WAITING_FOR_SPACE.value,
                                    JobStatus.CANCEL_REQUESTED.value,
                                ]
                            ),
                            DownloadJob.status.in_(
                                [JobStatus.NEEDS_REVIEW.value, JobStatus.FAILED.value]
                            )
                            & DownloadJob.id.in_(
                                select(JobArtifact.job_id).where(
                                    JobArtifact.kind == "completed_media",
                                    JobArtifact.status == "ready",
                                    JobArtifact.updated_at >= utc_now() - timedelta(days=7),
                                )
                            ),
                        )
                    )
                )
            )

    def adopt_published_jobs(self, music_root: Path) -> int:
        """Complete or relink jobs whose tagged publication is present in the index."""

        adopted = 0
        with self.session_factory() as session:
            tracks = list(session.scalars(select(Track).where(Track.provenance_json != "{}")))
            by_job: dict[str, list[Track]] = {}
            for track in tracks:
                try:
                    provenance = json.loads(track.provenance_json or "{}")
                except json.JSONDecodeError:
                    continue
                job_id = provenance.get("job_id") if isinstance(provenance, dict) else None
                if isinstance(job_id, str) and job_id:
                    by_job.setdefault(job_id, []).append(track)
            jobs = list(
                session.scalars(
                    select(DownloadJob).where(
                        DownloadJob.id.in_(by_job),
                        or_(
                            DownloadJob.status != JobStatus.COMPLETED.value,
                            DownloadJob.final_track_id.is_(None),
                        ),
                    )
                )
            )
            for job in jobs:
                candidates = by_job[job.id]
                preferred = [
                    track
                    for track in candidates
                    if track.id == job.final_track_id or track.filepath == job.final_relative_path
                ]
                if len(preferred) == 1:
                    track = preferred[0]
                elif len(candidates) == 1:
                    track = candidates[0]
                else:
                    # Copied provenance does not authorize an arbitrary relink.
                    continue
                relative = validate_relative_path(track.filepath)
                path = music_root.resolve(strict=True).joinpath(*relative.parts)
                if path.is_symlink() or not path.is_file():
                    continue
                digest = track.file_sha256 or sha256_file(path)
                track.file_sha256 = digest
                already_completed = job.status == JobStatus.COMPLETED.value
                job.status = JobStatus.COMPLETED.value
                job.stage = JobStage.COMPLETED.value
                job.progress = 1.0
                job.final_track_id = track.id
                job.final_relative_path = relative.as_posix()
                job.final_sha256 = digest
                if job.completed_at is None:
                    job.completed_at = utc_now()
                job.error_code = None
                job.error_message = None
                job.lease_token = None
                job.lease_expires_at = None
                job.warnings_json = _without_job_warning(
                    job.warnings_json,
                    "index_reconciliation_pending",
                )
                _job_event(
                    session,
                    job.id,
                    "job.reconciled" if already_completed else "job.completed",
                    (
                        "Linked a completed download to its library track"
                        if already_completed
                        else "Recovered an already-published download"
                    ),
                    details={"stage": JobStage.COMPLETED.value, "progress": 1.0},
                )
                adopted += 1
            session.commit()
        return adopted


class ServiceTaskQueue:
    """Fenced queue for cross-service work that uses the same SQLite database."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        target: TaskTarget | str,
        lease_seconds: int = 120,
    ) -> None:
        self.session_factory = session_factory
        self.target = target.value if isinstance(target, TaskTarget) else target
        self.lease_seconds = lease_seconds

    def ensure_scheduled_library_scan(self, *, now: datetime | None = None) -> str:
        """Queue one incremental scan unless any library scan is already active."""

        if self.target != TaskTarget.WORKER.value:
            raise ValueError("library scans can only target the worker service")
        timestamp = now or utc_now()
        with self.session_factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            existing = session.scalar(
                select(ServiceTask.id)
                .where(
                    ServiceTask.target == TaskTarget.WORKER.value,
                    ServiceTask.kind == "library_scan",
                    ServiceTask.state.in_(
                        [
                            TaskState.QUEUED.value,
                            TaskState.RUNNING.value,
                            TaskState.RETRY_WAIT.value,
                        ]
                    ),
                )
                .order_by(ServiceTask.created_at)
                .limit(1)
            )
            if existing is not None:
                return str(existing)
            task = ServiceTask(
                target=TaskTarget.WORKER.value,
                kind="library_scan",
                payload_version=1,
                payload_json=json.dumps({"full": False, "scheduled": True}, separators=(",", ":")),
                state=TaskState.QUEUED.value,
                available_at=timestamp,
            )
            session.add(task)
            session.flush()
            session.commit()
            return task.id

    def defer_library_scan(self, lease: ServiceTaskLease) -> None:
        """A busy singleton scanner is contention, not a provider failure."""
        if lease.kind != "library_scan":
            raise ValueError("only scans may use the scan contention defer")
        with self.session_factory.begin() as session:
            result = session.execute(
                update(ServiceTask)
                .where(
                    ServiceTask.id == lease.task_id,
                    ServiceTask.lease_token == lease.token,
                    ServiceTask.state == "running",
                )
                .values(
                    state="retry_wait",
                    available_at=utc_now() + timedelta(seconds=15),
                    lease_token=None,
                    lease_expires_at=None,
                    attempts=func.max(0, ServiceTask.attempts - 1),
                )
            )
            if result.rowcount != 1:
                raise LeaseLostError("scan task lease lost")

    def claim_next(self, *, now: datetime | None = None) -> ServiceTaskLease | None:
        timestamp = now or utc_now()
        token = secrets.token_hex(32)
        eligible = (
            (ServiceTask.target == self.target)
            & ServiceTask.state.in_([TaskState.QUEUED.value, TaskState.RETRY_WAIT.value])
            & (ServiceTask.available_at <= timestamp)
            & or_(ServiceTask.lease_token.is_(None), ServiceTask.lease_expires_at < timestamp)
        )
        candidate = (
            select(ServiceTask.id)
            .where(eligible)
            .order_by(ServiceTask.created_at.asc(), ServiceTask.id.asc())
            .limit(1)
            .scalar_subquery()
        )
        with self.session_factory() as session:
            row = (
                session.execute(
                    update(ServiceTask)
                    .where(ServiceTask.id == candidate, eligible)
                    .values(
                        state=TaskState.RUNNING.value,
                        lease_token=token,
                        lease_expires_at=timestamp + timedelta(seconds=self.lease_seconds),
                        attempts=ServiceTask.attempts + 1,
                        updated_at=timestamp,
                    )
                    .returning(
                        ServiceTask.id,
                        ServiceTask.lease_token,
                        ServiceTask.kind,
                        ServiceTask.payload_version,
                        ServiceTask.payload_json,
                        ServiceTask.attempts,
                    )
                )
                .mappings()
                .one_or_none()
            )
            session.commit()
        if row is None:
            return None
        return ServiceTaskLease(
            task_id=str(row["id"]),
            token=str(row["lease_token"]),
            kind=str(row["kind"]),
            payload_version=int(row["payload_version"]),
            payload=_decode_object(str(row["payload_json"]), label="service task payload"),
            attempts=int(row["attempts"]),
        )

    def complete(self, lease: ServiceTaskLease, result: dict[str, Any]) -> None:
        timestamp = utc_now()
        with self.session_factory() as session:
            changed = session.execute(
                update(ServiceTask)
                .where(
                    ServiceTask.id == lease.task_id,
                    ServiceTask.lease_token == lease.token,
                    ServiceTask.state == TaskState.RUNNING.value,
                )
                .values(
                    state=TaskState.COMPLETED.value,
                    result_json=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise LeaseLostError("service-task lease was lost before completion")
            session.commit()

    def heartbeat(self, lease: ServiceTaskLease, *, now: datetime | None = None) -> None:
        timestamp = now or utc_now()
        with self.session_factory() as session:
            changed = session.execute(
                update(ServiceTask)
                .where(
                    ServiceTask.id == lease.task_id,
                    ServiceTask.lease_token == lease.token,
                    ServiceTask.state == TaskState.RUNNING.value,
                )
                .values(
                    lease_expires_at=timestamp + timedelta(seconds=self.lease_seconds),
                    updated_at=timestamp,
                )
            ).rowcount
            if changed != 1:
                session.rollback()
                raise LeaseLostError("service-task lease was lost while heartbeating")
            session.commit()

    def release_for_shutdown(
        self,
        lease: ServiceTaskLease,
        *,
        now: datetime | None = None,
    ) -> None:
        """Release planned-shutdown work without consuming an attempt."""

        timestamp = now or utc_now()
        with self.session_factory.begin() as session:
            task = session.scalar(
                select(ServiceTask).where(
                    ServiceTask.id == lease.task_id,
                    ServiceTask.lease_token == lease.token,
                    ServiceTask.state == TaskState.RUNNING.value,
                )
            )
            if task is None:
                raise LeaseLostError("service-task lease was lost during worker shutdown")
            task.state = TaskState.RETRY_WAIT.value
            task.available_at = timestamp
            task.attempts = max(0, task.attempts - 1)
            task.lease_token = None
            task.lease_expires_at = None
            task.updated_at = timestamp

    def fail(
        self,
        lease: ServiceTaskLease,
        message: str,
        *,
        retryable: bool,
        max_attempts: int = 4,
    ) -> bool:
        """Record a fenced failure and return whether it is terminal."""

        timestamp = utc_now()
        retry = retryable and lease.attempts < max_attempts
        values: dict[str, Any] = {
            "state": TaskState.RETRY_WAIT.value if retry else TaskState.FAILED.value,
            "last_error": message[:1000],
            "lease_token": None,
            "lease_expires_at": None,
            "updated_at": timestamp,
        }
        if retry:
            values["available_at"] = timestamp + timedelta(
                seconds=_jittered_backoff(15, lease.attempts - 1, 900)
            )
        with self.session_factory() as session:
            changed = session.execute(
                update(ServiceTask)
                .where(
                    ServiceTask.id == lease.task_id,
                    ServiceTask.lease_token == lease.token,
                    ServiceTask.state == TaskState.RUNNING.value,
                )
                .values(**values)
            ).rowcount
            if changed != 1:
                session.rollback()
                raise LeaseLostError("service-task lease was lost while failing")
            session.commit()
        return not retry

    def recover_expired(self, *, max_attempts: int = 4, now: datetime | None = None) -> int:
        timestamp = now or utc_now()
        recovered = 0
        with self.session_factory() as session:
            tasks = session.execute(
                select(ServiceTask).where(
                    ServiceTask.target == self.target,
                    ServiceTask.state == TaskState.RUNNING.value,
                    ServiceTask.lease_expires_at.is_not(None),
                    ServiceTask.lease_expires_at < timestamp,
                )
            ).scalars()
            for task in tasks:
                task.state = (
                    TaskState.RETRY_WAIT.value
                    if task.attempts < max_attempts
                    else TaskState.FAILED.value
                )
                task.available_at = timestamp + timedelta(
                    seconds=_jittered_backoff(15, task.attempts, 900)
                )
                task.last_error = "worker lease expired"
                task.lease_token = None
                task.lease_expires_at = None
                task.updated_at = timestamp
                if task.state == TaskState.FAILED.value and task.kind == "resolve_direct_request":
                    try:
                        payload = _decode_object(task.payload_json, label="service-task payload")
                    except ValueError:
                        payload = {}
                    request_id = payload.get("request_id")
                    request = (
                        session.get(Request, request_id)
                        if isinstance(request_id, str) and request_id
                        else None
                    )
                    if request is not None and request.status not in {"auto_queued", "queued"}:
                        request.status = "failed"
                        request.error_code = "direct_resolution_failed"
                        request.error_message = (
                            "Direct YouTube validation could not resume after worker failure."
                        )
                        session.add(
                            make_event(
                                session,
                                entity_type="request",
                                entity_id=request.id,
                                event_type="request.direct_failed",
                                message="Direct YouTube request failed validation",
                                details_json=json.dumps(
                                    {"error_code": "direct_resolution_failed"},
                                    separators=(",", ":"),
                                ),
                            )
                        )
                recovered += 1
            session.commit()
        return recovered
