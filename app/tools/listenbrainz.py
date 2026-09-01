from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field

from app.clients.listenbrainz import ListenBrainzClient
from app.clients.openai import strict_json_schema
from app.schemas import StrictModel
from app.tools.registry import ToolDefinition, ToolRegistry


class PopularRecordingsArguments(StrictModel):
    artist_mbid: str | None = Field(min_length=36, max_length=36)
    period: Literal[
        "week",
        "month",
        "quarter",
        "half_yearly",
        "year",
        "all_time",
        "this_week",
        "this_month",
        "this_year",
    ]
    limit: int = Field(ge=1, le=100)


class ArtistRadioArguments(StrictModel):
    artist_mbid: str = Field(min_length=36, max_length=36)
    mode: Literal["easy", "medium", "hard"]
    max_similar_artists: int = Field(ge=1, le=25)
    max_recordings_per_artist: int = Field(ge=1, le=10)


class UserRecommendationsArguments(StrictModel):
    count: int = Field(ge=1, le=100)


def register_listenbrainz_tools(
    registry: ToolRegistry,
    client: ListenBrainzClient,
    *,
    default_username: str | None,
) -> None:
    async def popular(arguments: dict[str, Any]) -> Any:
        values = PopularRecordingsArguments.model_validate(arguments)
        if values.artist_mbid:
            rows = await client.top_recordings_for_artist(values.artist_mbid)
            return {
                "scope": "artist",
                "period": "all_time",
                "items": [
                    item
                    for row in rows[: values.limit]
                    if (item := _popular_recording(row)) is not None
                ],
            }
        return _sitewide_recordings(
            await client.sitewide_top(
                "recordings", range_name=values.period, count=values.limit, offset=0
            ),
            period=values.period,
            limit=values.limit,
        )

    async def radio(arguments: dict[str, Any]) -> Any:
        values = ArtistRadioArguments.model_validate(arguments)
        payload = await client.artist_radio(
            values.artist_mbid,
            mode=values.mode,
            max_similar_artists=values.max_similar_artists,
            max_recordings_per_artist=values.max_recordings_per_artist,
            pop_begin=0,
            pop_end=100,
        )
        return _radio_recordings(
            payload,
            mode=values.mode,
            max_artists=values.max_similar_artists,
            max_per_artist=values.max_recordings_per_artist,
        )

    async def recommendations(arguments: dict[str, Any]) -> Any:
        username = (default_username or "").strip()
        if not username:
            raise ValueError("ListenBrainz username is not configured")
        values = UserRecommendationsArguments.model_validate(arguments)
        return _recommendations(
            await client.recommendations(username, count=values.count, offset=0),
            count=values.count,
        )

    tools = (
        ToolDefinition(
            name="listenbrainz_popular_recordings",
            description=(
                "Get up to 100 popular recordings sitewide for a period, or all-time for one "
                "MusicBrainz artist MBID."
            ),
            parameters=strict_json_schema(PopularRecordingsArguments.model_json_schema()),
            handler=popular,
            max_result_bytes=64_000,
            cache_ttl_seconds=3_600,
        ),
        ToolDefinition(
            name="listenbrainz_artist_radio",
            description=(
                "Discover a bounded set of recordings from artists similar to one MusicBrainz "
                "artist MBID."
            ),
            parameters=strict_json_schema(ArtistRadioArguments.model_json_schema()),
            handler=radio,
            max_result_bytes=64_000,
            cache_ttl_seconds=3_600,
        ),
        ToolDefinition(
            name="listenbrainz_user_recommendations",
            description=(
                "Get up to 100 collaborative-filtering recording recommendations for the "
                "configured ListenBrainz username."
            ),
            parameters=strict_json_schema(UserRecommendationsArguments.model_json_schema()),
            handler=recommendations,
            max_result_bytes=64_000,
            cache_ttl_seconds=1_800,
        ),
    )
    for tool in tools:
        registry.register(tool)


def _popular_recording(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    recording_mbid = _mbid_text(value.get("recording_mbid"))
    if not recording_mbid:
        return None
    length = value.get("length")
    duration_seconds: float | None = None
    if isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0:
        duration_seconds = round(float(length) / 1000, 3)
    artist_mbids = value.get("artist_mbids")
    return {
        "recording_mbid": recording_mbid,
        "recording_name": _text(value.get("recording_name"), 300),
        "artist_name": _text(value.get("artist_name"), 300),
        "artist_mbids": _mbid_list(artist_mbids),
        "release_mbid": _mbid_text(value.get("release_mbid")),
        "release_name": _text(value.get("release_name"), 300),
        "duration_seconds": duration_seconds,
        "listen_count": _nonnegative_number(value.get("total_listen_count")),
        "listener_count": _nonnegative_number(value.get("total_user_count")),
    }


def _sitewide_recordings(
    payload: object,
    *,
    period: str,
    limit: int,
) -> dict[str, Any]:
    body = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        return {"scope": "sitewide", "period": period, "items": []}
    rows = body.get("recordings")
    items: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            recording_mbid = _mbid_text(row.get("recording_mbid"))
            artist_mbids = row.get("artist_mbids")
            items.append(
                {
                    "recording_mbid": recording_mbid,
                    "track_name": _text(row.get("track_name"), 300),
                    "artist_name": _text(row.get("artist_name"), 300),
                    "artist_mbids": _mbid_list(artist_mbids),
                    "release_mbid": _mbid_text(row.get("release_mbid")),
                    "release_name": _text(row.get("release_name"), 300),
                    "listen_count": _nonnegative_number(row.get("listen_count")),
                }
            )
    return {
        "scope": "sitewide",
        "period": _text(body.get("range"), 30) or period,
        "last_updated": _nonnegative_number(body.get("last_updated")),
        "total_recording_count": _nonnegative_number(body.get("total_recording_count")),
        "items": items,
    }


def _radio_recordings(
    payload: object,
    *,
    mode: str,
    max_artists: int,
    max_per_artist: int,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    artist_counts: dict[str, int] = {}
    for row in _walk_recording_rows(payload):
        recording_mbid = _mbid_text(row.get("recording_mbid"))
        artist_mbid = _mbid_text(row.get("similar_artist_mbid"))
        artist_name = _text(row.get("similar_artist_name"), 300)
        if not recording_mbid:
            continue
        artist_key = artist_mbid or artist_name
        if not artist_key:
            continue
        if artist_key not in artist_counts and len(artist_counts) >= max_artists:
            continue
        count = artist_counts.get(artist_key, 0)
        if count >= max_per_artist:
            continue
        artist_counts[artist_key] = count + 1
        items.append(
            {
                "recording_mbid": recording_mbid,
                "similar_artist_mbid": artist_mbid,
                "similar_artist_name": artist_name,
                "listen_count": _nonnegative_number(row.get("total_listen_count")),
            }
        )
    return {
        "mode": mode,
        "artist_count": len(artist_counts),
        "items": items,
    }


def _recommendations(payload: object, *, count: int) -> dict[str, Any]:
    body = payload.get("payload") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        return {"items": []}
    rows = body.get("mbids")
    items: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:count]:
            if not isinstance(row, dict):
                continue
            recording_mbid = _mbid_text(row.get("recording_mbid"))
            if recording_mbid:
                items.append(
                    {
                        "recording_mbid": recording_mbid,
                        "score": _number(row.get("score")),
                    }
                )
    return {
        "user_name": _text(body.get("user_name"), 128),
        "last_updated": _nonnegative_number(body.get("last_updated")),
        "items": items,
    }


def _walk_recording_rows(value: object, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        if "recording_mbid" in value:
            return [value]
        rows: list[dict[str, Any]] = []
        for child in value.values():
            rows.extend(_walk_recording_rows(child, depth=depth + 1))
        return rows
    if isinstance(value, list):
        rows = []
        for child in value:
            rows.extend(_walk_recording_rows(child, depth=depth + 1))
        return rows
    return []


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] or None


def _mbid_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _mbid_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [mbid for item in value[:10] if (mbid := _mbid_text(item)) is not None]


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _nonnegative_number(value: object) -> int | float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None
