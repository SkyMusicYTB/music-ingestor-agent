from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Album, ScanRun, ServiceTask, Track
from app.services.duplicates import normalize_text
from app.services.library_scan import scan_has_coverage


@dataclass(frozen=True)
class LibraryPage:
    items: list[Track]
    total: int
    page: int
    page_size: int
    format_counts: dict[str, int] = field(default_factory=dict)
    codec_counts: dict[str, int] = field(default_factory=dict)


class LibraryRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def search(
        self,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
        *,
        format: str | None = None,
        codec: str | None = None,
        presence: str = "present",
    ) -> LibraryPage:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        with self.factory() as session:
            if presence not in {"present", "missing", "all"}:
                raise ValueError("unknown library presence filter")
            statement = select(Track).outerjoin(Album, Track.album_id == Album.id)
            if presence != "all":
                statement = statement.where(Track.is_present == (presence == "present"))
            if query.strip():
                normalized = f"%{normalize_text(query)}%"
                predicate = or_(
                    Track.artist_normalized.like(normalized),
                    Track.title_normalized.like(normalized),
                    Album.title_normalized.like(normalized),
                    func.lower(Track.album).like(f"%{query.strip().casefold()}%"),
                )
                statement = statement.where(predicate)
            matching = statement.subquery()
            format_counts = {
                str(row[0] or "unknown"): int(row[1])
                for row in session.execute(
                    select(matching.c.file_extension, func.count()).group_by(
                        matching.c.file_extension
                    )
                )
            }
            codec_counts = {
                str(row[0] or "unknown"): int(row[1])
                for row in session.execute(
                    select(matching.c.codec, func.count()).group_by(matching.c.codec)
                )
            }
            if format:
                normalized_format = format.casefold().lstrip(".")
                statement = statement.where(
                    Track.file_extension.is_(None)
                    if normalized_format == "unknown"
                    else Track.file_extension == "." + normalized_format
                )
            if codec:
                statement = statement.where(
                    Track.codec.is_(None)
                    if codec.casefold() == "unknown"
                    else Track.codec == codec.casefold()
                )
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            items = list(
                session.scalars(
                    statement.order_by(
                        Track.artist_normalized,
                        Track.album,
                        Track.disc_number,
                        Track.track_number,
                        Track.title_normalized,
                        Track.id,
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return LibraryPage(items, total, page, page_size, format_counts, codec_counts)

    def summary(self, sample_limit: int = 20) -> dict[str, object]:
        with self.factory() as session:
            track_count = int(
                session.scalar(select(func.count()).select_from(Track).where(Track.is_present)) or 0
            )
            album_count = int(session.scalar(select(func.count()).select_from(Album)) or 0)
            artists = session.execute(
                select(Track.artist, func.count(Track.id))
                .where(Track.is_present)
                .group_by(Track.artist_normalized)
                .order_by(func.count(Track.id).desc())
                .limit(max(1, min(sample_limit, 50)))
            ).all()
            return {
                "track_count": track_count,
                "album_count": album_count,
                "top_artists": [{"artist": row[0], "count": row[1]} for row in artists],
            }

    def initial_scan_complete(self) -> bool:
        with self.factory() as session:
            return any(
                scan_has_coverage(scan)
                for scan in session.scalars(
                    select(ScanRun).where(ScanRun.kind == "initial", ScanRun.status == "completed")
                )
            )

    def scan_status(self, *, include_details: bool = False) -> dict[str, object]:
        with self.factory() as session:
            latest = session.scalar(select(ScanRun).order_by(ScanRun.generation.desc()).limit(1))
            task = session.scalar(
                select(ServiceTask)
                .where(
                    ServiceTask.kind == "library_scan",
                    ServiceTask.target == "worker",
                    ServiceTask.state.in_(["queued", "running", "retry_wait"]),
                )
                .order_by(ServiceTask.created_at)
                .limit(1)
            )
            result: dict[str, object] = {"state": "not_started", "scan": None}
            if latest:
                result["state"] = latest.status
                result["scan"] = scan_payload(latest, include_details=include_details)
            if task:
                result["state"] = task.state
                result["task_id"] = task.id
            return result


def scan_payload(scan: ScanRun, *, include_details: bool = False) -> dict[str, object]:
    try:
        summary = json.loads(scan.summary_json or "{}")
    except ValueError:
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    if not include_details:
        summary.pop("samples", None)
    return {
        "id": scan.id,
        "kind": scan.kind,
        "status": scan.status,
        "started_at": scan.started_at.isoformat(),
        "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        "scanned_files": scan.scanned_files,
        "changed_files": scan.changed_files,
        "missing_files": scan.missing_files,
        "error_count": scan.error_count,
        "traversal_complete": scan.traversal_complete,
        "summary": summary,
    }
