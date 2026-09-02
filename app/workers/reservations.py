from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, insert, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.db.ids import uuid7
from app.db.models import DownloadJob, JobArtifact, utc_now
from app.services.filesystem import InsufficientSpaceError


class MediaBudgetExceeded(RuntimeError):
    pass


class ReservationLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Reservation:
    job_id: str
    lease_token: str
    kind: str
    filesystem_key: str
    maximum_footprint: int
    generation_token: str


class MediaReservationManager:
    """Durable, cross-process reservations backed by SQLite ``BEGIN IMMEDIATE``.

    Free space already accounts for bytes written. Reservation rows account only
    for each active job's remaining headroom, preventing parallel workers from all
    promising the same bytes. Retained retry data is included when headroom is
    recalculated on the next admission.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        min_free_bytes: int,
        max_media_bytes: int,
        normalization_overhead_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        bind = factory.kw.get("bind")
        if not isinstance(bind, Engine):
            raise TypeError("media reservations require a bound SQLAlchemy engine")
        self.engine = bind
        self.min_free_bytes = min_free_bytes
        self.max_media_bytes = max_media_bytes
        self.maximum_staging_footprint = max_media_bytes * 2 + normalization_overhead_bytes

    def reserve_download(self, job_id: str, lease_token: str, staging: Path) -> Reservation:
        return self._reserve(
            job_id,
            lease_token,
            kind="download_reservation",
            root=staging,
            maximum_footprint=self.maximum_staging_footprint,
            current_bytes=tree_size(staging),
        )

    def reserve_publication(
        self, job_id: str, lease_token: str, music_root: Path, size_bytes: int
    ) -> Reservation:
        if size_bytes <= 0 or size_bytes > self.max_media_bytes:
            raise MediaBudgetExceeded("publication size is outside the configured media limit")
        return self._reserve(
            job_id,
            lease_token,
            kind="publication_reservation",
            root=music_root,
            maximum_footprint=size_bytes,
            current_bytes=0,
        )

    def update_download(self, reservation: Reservation, staging: Path) -> int:
        current = tree_size(staging)
        if current > reservation.maximum_footprint:
            raise MediaBudgetExceeded("job staging exceeded its hard byte ceiling")
        self._set_headroom(
            reservation,
            root=staging,
            remaining=max(0, reservation.maximum_footprint - current),
        )
        return current

    def release(self, reservation: Reservation) -> None:
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            connection.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.job_id == reservation.job_id,
                    JobArtifact.kind == reservation.kind,
                    JobArtifact.relative_path == reservation.filesystem_key,
                    JobArtifact.status == "creating",
                    JobArtifact.generation_token == reservation.generation_token,
                )
                .values(status="removed", size_bytes=0, updated_at=utc_now())
            )
            connection.commit()

    def _reserve(
        self,
        job_id: str,
        lease_token: str,
        *,
        kind: str,
        root: Path,
        maximum_footprint: int,
        current_bytes: int,
    ) -> Reservation:
        resolved = root.resolve(strict=True)
        filesystem_key = f"device:{resolved.stat().st_dev}"
        remaining = max(0, maximum_footprint - current_bytes)
        generation_token = secrets.token_hex(32)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            timestamp = utc_now()
            valid_lease = connection.execute(
                select(DownloadJob.id).where(
                    DownloadJob.id == job_id,
                    DownloadJob.status == "active",
                    DownloadJob.lease_token == lease_token,
                    DownloadJob.lease_expires_at.is_not(None),
                    DownloadJob.lease_expires_at >= timestamp,
                )
            ).scalar_one_or_none()
            if valid_lease is None:
                connection.rollback()
                raise ReservationLost("job lease is no longer active for reservation admission")
            # Crash leftovers from jobs that no longer own a running lease do not
            # reserve future headroom; their retained bytes are already reflected
            # by disk_usage and are measured again when that job is admitted.
            connection.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.kind.in_(["download_reservation", "publication_reservation"]),
                    JobArtifact.status == "creating",
                    JobArtifact.job_id.in_(
                        select(DownloadJob.id).where(
                            or_(
                                DownloadJob.status.not_in(["active", "cancel_requested"]),
                                DownloadJob.lease_token.is_(None),
                                DownloadJob.lease_expires_at.is_(None),
                                DownloadJob.lease_expires_at < timestamp,
                            )
                        )
                    ),
                )
                .values(status="removed", size_bytes=0, updated_at=timestamp)
            )
            own_id = connection.execute(
                select(JobArtifact.id).where(
                    JobArtifact.job_id == job_id,
                    JobArtifact.kind == kind,
                    JobArtifact.relative_path == filesystem_key,
                )
            ).scalar_one_or_none()
            reserved_elsewhere = connection.execute(
                select(JobArtifact.size_bytes).where(
                    JobArtifact.status == "creating",
                    JobArtifact.relative_path == filesystem_key,
                    JobArtifact.kind.in_(["download_reservation", "publication_reservation"]),
                    JobArtifact.job_id != job_id,
                )
            ).scalars()
            promised = sum(int(value or 0) for value in reserved_elsewhere)
            free = shutil.disk_usage(resolved).free
            if free - promised - remaining < self.min_free_bytes:
                connection.rollback()
                raise InsufficientSpaceError(
                    "parallel media reservations would cross the free-space reserve"
                )
            metadata = json.dumps(
                {
                    "root": str(resolved),
                    "maximum_footprint": maximum_footprint,
                    "current_bytes": current_bytes,
                },
                separators=(",", ":"),
            )
            if own_id is None:
                connection.execute(
                    insert(JobArtifact).values(
                        id=uuid7(),
                        job_id=job_id,
                        kind=kind,
                        stage="downloading" if kind == "download_reservation" else "publishing",
                        relative_path=filesystem_key,
                        size_bytes=remaining,
                        generation_token=generation_token,
                        status="creating",
                        metadata_json=metadata,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            else:
                connection.execute(
                    update(JobArtifact)
                    .where(JobArtifact.id == own_id)
                    .values(
                        size_bytes=remaining,
                        generation_token=generation_token,
                        status="creating",
                        metadata_json=metadata,
                        updated_at=timestamp,
                    )
                )
            connection.commit()
        return Reservation(
            job_id,
            lease_token,
            kind,
            filesystem_key,
            maximum_footprint,
            generation_token,
        )

    def _set_headroom(self, reservation: Reservation, *, root: Path, remaining: int) -> None:
        resolved = root.resolve(strict=True)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            timestamp = utc_now()
            valid_lease = connection.execute(
                select(DownloadJob.id).where(
                    DownloadJob.id == reservation.job_id,
                    DownloadJob.status == "active",
                    DownloadJob.lease_token == reservation.lease_token,
                    DownloadJob.lease_expires_at.is_not(None),
                    DownloadJob.lease_expires_at >= timestamp,
                )
            ).scalar_one_or_none()
            if valid_lease is None:
                connection.rollback()
                raise ReservationLost("job lease is no longer active for reservation update")
            promised = sum(
                int(value or 0)
                for value in connection.execute(
                    select(JobArtifact.size_bytes).where(
                        JobArtifact.status == "creating",
                        JobArtifact.relative_path == reservation.filesystem_key,
                        JobArtifact.kind.in_(["download_reservation", "publication_reservation"]),
                        JobArtifact.job_id != reservation.job_id,
                    )
                ).scalars()
            )
            if shutil.disk_usage(resolved).free - promised - remaining < self.min_free_bytes:
                connection.rollback()
                raise InsufficientSpaceError("media job crossed the shared free-space reserve")
            updated = connection.execute(
                update(JobArtifact)
                .where(
                    JobArtifact.job_id == reservation.job_id,
                    JobArtifact.kind == reservation.kind,
                    JobArtifact.relative_path == reservation.filesystem_key,
                    JobArtifact.status == "creating",
                    JobArtifact.generation_token == reservation.generation_token,
                )
                .values(size_bytes=remaining, updated_at=timestamp)
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise ReservationLost("media reservation generation is no longer active")
            connection.commit()


class BudgetGuardSignal:
    def __init__(
        self,
        base: object,
        manager: MediaReservationManager,
        reservation: Reservation,
        staging: Path,
        *,
        interval_seconds: float = 0.25,
    ) -> None:
        self.base = base
        self.manager = manager
        self.reservation = reservation
        self.staging = staging
        self.interval_seconds = interval_seconds
        self._last_check = 0.0
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        base_is_set = getattr(self.base, "is_set", None)
        if callable(base_is_set) and bool(base_is_set()):
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._last_check >= self.interval_seconds:
                self.manager.update_download(self.reservation, self.staging)
                self._last_check = now
        return False


def tree_size(root: Path, *, max_entries: int = 10_000) -> int:
    resolved = root.resolve(strict=True)
    total = 0
    entries = 0
    pending = [resolved]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries += 1
                if entries > max_entries:
                    raise MediaBudgetExceeded("job staging contains too many filesystem entries")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise MediaBudgetExceeded("job staging contains a symbolic link")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise MediaBudgetExceeded("job staging contains an unsupported file type")
    return total
