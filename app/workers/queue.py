from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.enums import JobStage, JobStatus, TaskState, TaskTarget
from app.db.models import DownloadJob, Event, JobReviewOption, Request, ServiceTask, Track
from app.services.filesystem import sha256_file, validate_relative_path


class LeaseLostError(RuntimeError):
    """The worker no longer owns the fencing token for a job."""


class JobCancellationRequested(RuntimeError):
    pass


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
        Event(
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
                error_code=None,
                error_message=None,
                updated_at=timestamp,
            )
            .returning(
                DownloadJob.id,
                DownloadJob.lease_token,
                DownloadJob.approved_snapshot_json,
                DownloadJob.retry_count,
            )
        )
        with self.session_factory() as session:
            row = session.execute(statement).mappings().one_or_none()
            if row is not None:
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
        now: datetime | None = None,
    ) -> None:
        timestamp = now or utc_now()
        serialized_warnings = json.dumps(
            [{"code": "needs_review", "message": reason[:500]}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.session_factory() as session:
            result = session.execute(
                update(DownloadJob)
                .where(
                    DownloadJob.id == lease.job_id,
                    DownloadJob.lease_token == lease.token,
                    DownloadJob.status == JobStatus.ACTIVE.value,
                )
                .values(
                    status=JobStatus.NEEDS_REVIEW.value,
                    stage=JobStage.RESOLVING_SOURCE.value,
                    lease_token=None,
                    lease_expires_at=None,
                    warnings_json=serialized_warnings,
                    error_code="source_needs_review",
                    error_message=reason[:1000],
                    updated_at=timestamp,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise LeaseLostError("job lease was lost while requesting review")
            session.query(JobReviewOption).filter(JobReviewOption.job_id == lease.job_id).delete()
            for ordinal, option in enumerate((options or [])[:10], start=1):
                rank_value = option.get("rank", ordinal)
                score_value = option.get("score", 0.0)
                rank = rank_value if isinstance(rank_value, int) and rank_value > 0 else ordinal
                score = (
                    float(score_value)
                    if isinstance(score_value, (int, float)) and not isinstance(score_value, bool)
                    else 0.0
                )
                kind = str(option.get("kind") or "source")[:32]
                payload = {key: value for key, value in option.items() if key != "score"}
                session.add(
                    JobReviewOption(
                        job_id=lease.job_id,
                        kind=kind,
                        rank=rank,
                        provider_payload_json=json.dumps(
                            payload, ensure_ascii=False, separators=(",", ":")
                        ),
                        score=max(0.0, min(1.0, score)),
                    )
                )
            _job_event(
                session,
                lease.job_id,
                "job.needs_review",
                "Download job needs review",
                details={"option_count": min(len(options or []), 10)},
            )
            session.commit()

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
                        DownloadJob.status.in_(
                            [
                                JobStatus.QUEUED.value,
                                JobStatus.ACTIVE.value,
                                JobStatus.RETRY_WAIT.value,
                                JobStatus.WAITING_FOR_SPACE.value,
                                JobStatus.CANCEL_REQUESTED.value,
                            ]
                        )
                    )
                )
            )

    def adopt_published_jobs(self, music_root: Path) -> int:
        """Complete or relink jobs whose tagged publication is present in the index."""

        adopted = 0
        with self.session_factory() as session:
            tracks = list(session.scalars(select(Track).where(Track.provenance_json != "{}")))
            by_job: dict[str, Track] = {}
            for track in tracks:
                try:
                    provenance = json.loads(track.provenance_json or "{}")
                except json.JSONDecodeError:
                    continue
                job_id = provenance.get("job_id") if isinstance(provenance, dict) else None
                if isinstance(job_id, str) and job_id:
                    by_job[job_id] = track
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
                track = by_job[job.id]
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
        with self.session_factory.begin() as session:
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
            return task.id

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
                            Event(
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
