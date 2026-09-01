from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.openai import strict_json_schema
from app.db.models import Album, Track
from app.schemas import StrictModel
from app.services.duplicates import normalize_text
from app.tools.registry import ToolDefinition, ToolRegistry


class LibrarySearchArguments(StrictModel):
    query: str | None = Field(max_length=300)
    artist: str | None = Field(max_length=300)
    title: str | None = Field(max_length=300)
    album: str | None = Field(max_length=300)
    year_min: int | None = Field(ge=1800, le=2200)
    year_max: int | None = Field(ge=1800, le=2200)
    version: str | None = Field(max_length=100)
    limit: int = Field(ge=1, le=50)

    @model_validator(mode="after")
    def valid_years(self) -> LibrarySearchArguments:
        if self.year_min and self.year_max and self.year_min > self.year_max:
            raise ValueError("year_min cannot exceed year_max")
        return self


class LibrarySummaryArguments(StrictModel):
    focus_artists: list[str] = Field(max_length=20)
    top_artist_limit: int = Field(ge=1, le=30)
    sample_track_limit: int = Field(ge=0, le=50)


def register_library_tools(registry: ToolRegistry, session_factory: sessionmaker[Session]) -> None:
    async def search(arguments: dict[str, Any]) -> dict[str, Any]:
        values = LibrarySearchArguments.model_validate(arguments)
        with session_factory() as session:
            statement = select(Track).where(Track.is_present)
            if values.query:
                query = f"%{normalize_text(values.query)}%"
                statement = statement.where(
                    or_(
                        Track.artist_normalized.like(query),
                        Track.title_normalized.like(query),
                    )
                )
            if values.artist:
                statement = statement.where(
                    Track.artist_normalized.like(f"%{normalize_text(values.artist)}%")
                )
            if values.title:
                statement = statement.where(
                    Track.title_normalized.like(f"%{normalize_text(values.title)}%")
                )
            if values.album:
                statement = statement.where(Track.album.like(f"%{values.album.strip()}%"))
            if values.year_min:
                statement = statement.where(Track.year >= values.year_min)
            if values.year_max:
                statement = statement.where(Track.year <= values.year_max)
            if values.version:
                statement = statement.where(
                    Track.version_signature.like(f"%{normalize_text(values.version)}%")
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        Track.artist_normalized, Track.album, Track.track_number, Track.id
                    ).limit(values.limit)
                )
            )
        return {"items": [_track_result(item) for item in rows], "returned": len(rows)}

    async def summary(arguments: dict[str, Any]) -> dict[str, Any]:
        values = LibrarySummaryArguments.model_validate(arguments)
        with session_factory() as session:
            track_count = int(
                session.scalar(select(func.count()).select_from(Track).where(Track.is_present)) or 0
            )
            album_count = int(session.scalar(select(func.count()).select_from(Album)) or 0)
            artists = session.execute(
                select(Track.artist, func.count(Track.id))
                .where(Track.is_present)
                .group_by(Track.artist_normalized)
                .order_by(func.count(Track.id).desc(), Track.artist_normalized)
                .limit(values.top_artist_limit)
            ).all()
            focus: list[dict[str, Any]] = []
            for artist in values.focus_artists:
                normalized = normalize_text(artist)
                if not normalized:
                    continue
                count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(Track)
                        .where(Track.is_present, Track.artist_normalized == normalized)
                    )
                    or 0
                )
                focus.append({"artist": artist[:300], "track_count": count})
            samples = list(
                session.scalars(
                    select(Track)
                    .where(Track.is_present)
                    .order_by(Track.updated_at.desc(), Track.id)
                    .limit(values.sample_track_limit)
                )
            )
        return {
            "track_count": track_count,
            "album_count": album_count,
            "top_artists": [{"artist": row[0], "track_count": row[1]} for row in artists],
            "focus_artists": focus,
            "sample_tracks": [_track_result(item) for item in samples],
        }

    registry.register(
        ToolDefinition(
            name="search_library",
            description=(
                "Search up to 50 present tracks in the local library using bounded artist, title, "
                "album, year, and version filters. Filesystem paths are never returned."
            ),
            parameters=strict_json_schema(LibrarySearchArguments.model_json_schema()),
            handler=search,
            timeout_seconds=5.0,
            max_result_bytes=64_000,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_library_summary",
            description=(
                "Return library counts, bounded top/focus artist counts, and an optional bounded "
                "track sample without filesystem paths."
            ),
            parameters=strict_json_schema(LibrarySummaryArguments.model_json_schema()),
            handler=summary,
            timeout_seconds=5.0,
            max_result_bytes=64_000,
        )
    )


def _track_result(item: Track) -> dict[str, Any]:
    return {
        "id": item.id,
        "artist": item.artist,
        "title": item.title,
        "album": item.album,
        "album_artist": item.album_artist,
        "genre": item.genre,
        "year": item.year,
        "duration_seconds": item.duration_seconds,
        "recording_mbid": item.recording_mbid,
        "release_mbid": item.release_mbid,
        "release_group_mbid": item.release_group_mbid,
        "version": item.version_signature,
    }
