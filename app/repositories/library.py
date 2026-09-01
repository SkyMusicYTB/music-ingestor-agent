from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Album, ScanRun, Track
from app.services.duplicates import normalize_text


@dataclass(frozen=True)
class LibraryPage:
    items: list[Track]
    total: int
    page: int
    page_size: int


class LibraryRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def search(self, query: str = "", page: int = 1, page_size: int = 50) -> LibraryPage:
        page = max(1, page)
        page_size = max(1, min(page_size, 100))
        with self.factory() as session:
            statement = select(Track).where(Track.is_present)
            count_statement = select(func.count()).select_from(Track).where(Track.is_present)
            if query.strip():
                normalized = f"%{normalize_text(query)}%"
                predicate = or_(
                    Track.artist_normalized.like(normalized),
                    Track.title_normalized.like(normalized),
                )
                statement = statement.where(predicate)
                count_statement = count_statement.where(predicate)
            total = int(session.scalar(count_statement) or 0)
            items = list(
                session.scalars(
                    statement.order_by(Track.artist_normalized, Track.album, Track.track_number)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return LibraryPage(items, total, page, page_size)

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
            return (
                session.scalar(
                    select(ScanRun.id)
                    .where(ScanRun.kind == "initial", ScanRun.status == "completed")
                    .limit(1)
                )
                is not None
            )
