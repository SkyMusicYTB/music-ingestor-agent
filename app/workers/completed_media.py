"""Reuse completed, hash-verified job-local audio without trusting old paths."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import immediate_session
from app.db.models import DownloadJob, JobArtifact
from app.services.filesystem import sha256_file
from app.workers.cleanup import cleanup_staging_directory
from app.workers.queue import JobLease, LeaseLostError

MEDIA_RETENTION = timedelta(days=7)


class CompletedMediaStore:
    def __init__(self, factory: sessionmaker[Session], max_bytes: int) -> None:
        self.factory = factory
        self.max_bytes = max_bytes

    def find(
        self, lease: JobLease, staging: Path, *, extractor: str, source_id: str
    ) -> Path | None:
        with self.factory() as session:
            _fence(session, lease)
            artifacts = list(
                session.scalars(
                    select(JobArtifact).where(
                        JobArtifact.job_id == lease.job_id,
                        JobArtifact.kind == "completed_media",
                        JobArtifact.status == "ready",
                    )
                )
            )
        for artifact in artifacts:
            if (
                not artifact.content_sha256
                or re.fullmatch(r"[0-9a-f]{64}", artifact.content_sha256) is None
                or artifact.size_bytes is None
                or artifact.size_bytes <= 0
            ):
                continue
            try:
                metadata = json.loads(artifact.metadata_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                continue
            if (
                metadata.get("extractor") != extractor
                or metadata.get("source_id") != source_id
                or artifact.updated_at.replace(tzinfo=UTC) < datetime.now(UTC) - MEDIA_RETENTION
            ):
                continue
            path = staging / artifact.relative_path
            if self._valid(path, staging, artifact.size_bytes, artifact.content_sha256):
                with self.factory() as session:
                    _fence(session, lease)
                return path
        return None

    def save(
        self, lease: JobLease, path: Path, staging: Path, *, extractor: str, source_id: str
    ) -> None:
        if not self._valid(path, staging, None, None):
            raise ValueError("completed media must be a bounded job-local regular file")
        digest = sha256_file(path)
        size = path.stat().st_size
        # Audio is durable before the database says it can be reused.
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        with self.factory.begin() as session:
            _fence(session, lease)
            artifact = session.scalar(
                select(JobArtifact).where(
                    JobArtifact.job_id == lease.job_id,
                    JobArtifact.kind == "completed_media",
                    JobArtifact.relative_path == path.name,
                )
            )
            if artifact is None:
                artifact = JobArtifact(
                    job_id=lease.job_id, kind="completed_media", relative_path=path.name
                )
                session.add(artifact)
            artifact.stage = "verifying"
            artifact.status = "ready"
            artifact.generation_token = lease.token
            artifact.content_sha256 = digest
            artifact.size_bytes = size
            artifact.metadata_json = json.dumps(
                {"schema_version": 1, "extractor": extractor, "source_id": source_id},
                separators=(",", ":"),
            )
            artifact.updated_at = datetime.now(UTC)

    def invalidate(self, lease: JobLease) -> None:
        with self.factory.begin() as session:
            _fence(session, lease)
            for artifact in session.scalars(
                select(JobArtifact).where(
                    JobArtifact.job_id == lease.job_id,
                    JobArtifact.kind == "completed_media",
                    JobArtifact.status == "ready",
                )
            ):
                artifact.status = "invalid"

    def has_ready(self, job_id: str, *, include_expired: bool = False) -> bool:
        with self.factory() as session:
            statement = select(JobArtifact.id).where(
                JobArtifact.job_id == job_id,
                JobArtifact.kind == "completed_media",
                JobArtifact.status == "ready",
            )
            if not include_expired:
                statement = statement.where(
                    JobArtifact.updated_at >= datetime.now(UTC) - MEDIA_RETENTION
                )
            return session.scalar(statement.limit(1)) is not None

    def _valid(self, path: Path, staging: Path, size: int | None, digest: str | None) -> bool:
        try:
            if (
                path.parent != staging
                or path.name in {".", ".."}
                or staging.is_symlink()
                or path.is_symlink()
            ):
                return False
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 0 < before.st_size <= self.max_bytes
                or (size is not None and before.st_size != size)
            ):
                return False
            if digest is not None and sha256_file(path) != digest:
                return False
            after = path.lstat()
            return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            )
        except OSError:
            return False


def _fence(session: Session, lease: JobLease) -> None:
    if (
        session.scalar(
            select(DownloadJob.id).where(
                DownloadJob.id == lease.job_id,
                DownloadJob.lease_token == lease.token,
                DownloadJob.status == "active",
                DownloadJob.lease_expires_at >= datetime.now(UTC),
            )
        )
        is None
    ):
        raise LeaseLostError("job lease was lost while retaining completed media")


def expire_review_media(factory: sessionmaker[Session], root: Path) -> int:
    """Bound retention without deleting media from a concurrently resumed job."""
    removed = 0
    if root.is_symlink() or not root.is_dir():
        return removed
    with immediate_session(factory) as session:
        artifacts = list(
            session.scalars(
                select(JobArtifact)
                .join(DownloadJob)
                .where(
                    DownloadJob.status.in_(["needs_review", "failed", "cancelled", "completed"]),
                    JobArtifact.kind == "completed_media",
                    JobArtifact.status == "ready",
                    JobArtifact.updated_at < datetime.now(UTC) - MEDIA_RETENTION,
                    JobArtifact.job_id.not_in(
                        select(JobArtifact.job_id).where(
                            JobArtifact.kind == "completed_media",
                            JobArtifact.status == "ready",
                            JobArtifact.updated_at >= datetime.now(UTC) - MEDIA_RETENTION,
                        )
                    ),
                )
                .limit(100)
            )
        )
        seen: set[str] = set()
        for artifact in artifacts:
            if artifact.job_id in seen:
                continue
            seen.add(artifact.job_id)
            # The stored job UUID, not provider text, determines the isolated folder.
            try:
                job_id = str(uuid.UUID(artifact.job_id))
            except ValueError:
                continue
            if cleanup_staging_directory(root / job_id):
                for row in session.scalars(
                    select(JobArtifact).where(
                        JobArtifact.job_id == job_id,
                        JobArtifact.kind == "completed_media",
                        JobArtifact.status == "ready",
                    )
                ):
                    row.status = "removed"
                removed += 1
    return removed
