from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from app.clients.apple_metadata import AppleMetadataClient
from app.clients.musicbrainz import MusicBrainzClient, MusicBrainzError
from app.clients.openai import strict_json_schema
from app.schemas import StrictModel
from app.services.metadata_matching import (
    MetadataMatcher,
    ReleaseMetadataCandidate,
    ReleaseMetadataMatcher,
    candidates_from_apple,
    candidates_from_musicbrainz,
    select_sensible_release,
)
from app.tools.media_sources import current_tool_authorization
from app.tools.registry import ToolDefinition, ToolRegistry


class RecordingSearchArguments(StrictModel):
    artist: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    album: str | None = Field(max_length=300)
    duration_seconds: float | None = Field(ge=1, le=14_400)
    version: str | None = Field(max_length=100)
    limit: int = Field(ge=1, le=25)


class ReleaseSearchArguments(StrictModel):
    artist: str = Field(min_length=1, max_length=300)
    album: str = Field(min_length=1, max_length=300)
    year: int | None = Field(ge=1800, le=2200)
    limit: int = Field(ge=1, le=25)


def register_musicbrainz_tools(
    registry: ToolRegistry,
    client: MusicBrainzClient,
    apple_client: AppleMetadataClient | None = None,
) -> None:
    async def recordings(arguments: dict[str, Any]) -> dict[str, Any]:
        values = RecordingSearchArguments.model_validate(arguments)
        authorization = current_tool_authorization()
        explicit_album = authorization.requested_album if authorization is not None else None
        explicit_version = authorization.requested_version if authorization is not None else None
        payload = await client.search_recordings(
            artist=values.artist, title=values.title, limit=values.limit
        )
        candidates = candidates_from_musicbrainz(payload)
        fallback_used = False
        if not candidates and apple_client is not None:
            apple_payload = await apple_client.search_tracks(
                artist=values.artist, title=values.title, limit=values.limit
            )
            candidates = candidates_from_apple(apple_payload)
            fallback_used = True
        candidates = [
            select_sensible_release(candidate, requested_album=explicit_album)
            for candidate in candidates
        ]
        ranked = MetadataMatcher().rank(
            artist=values.artist,
            title=values.title,
            album=explicit_album or values.album,
            duration_seconds=values.duration_seconds,
            requested_version=explicit_version or values.version,
            album_is_explicit=explicit_album is not None,
            version_is_explicit=explicit_version is not None,
            candidates=candidates,
            limit=values.limit,
        )
        return {
            "fallback_used": fallback_used,
            "fallback_provider": "apple_search" if fallback_used else None,
            "matches": [
                {
                    "artist": match.candidate.artist,
                    "artists": list(match.candidate.artists),
                    "title": match.candidate.title,
                    "album": match.candidate.album,
                    "year": match.candidate.year,
                    "duration_seconds": match.candidate.duration_seconds,
                    "version": match.candidate.version,
                    "recording_mbid": match.candidate.recording_mbid,
                    "release_mbid": match.candidate.release_mbid,
                    "release_group_mbid": match.candidate.release_group_mbid,
                    "source": match.candidate.source,
                    "score": round(match.score, 2),
                    "decision": (
                        "review" if fallback_used and match.decision == "auto" else match.decision
                    ),
                    "association_scope": (
                        "review_only_apple_fallback" if fallback_used else "canonical_musicbrainz"
                    ),
                    "lead": round(match.lead, 2) if match.lead is not None else None,
                    "reasons": list(match.reasons),
                    "contradiction_codes": list(match.contradiction_codes),
                }
                for match in ranked
            ],
        }

    async def releases(arguments: dict[str, Any]) -> dict[str, Any]:
        values = ReleaseSearchArguments.model_validate(arguments)
        query = f'release:"{_lucene(values.album)}" AND artist:"{_lucene(values.artist)}"'
        if values.year is not None:
            query += f" AND date:{values.year}"
        payload = await client.search("release", query, limit=values.limit)
        rows = payload.get("releases", [])
        if not isinstance(rows, list):
            rows = []
        search_rows = [value for value in rows[: values.limit] if isinstance(value, Mapping)]
        hydrated: dict[str, dict[str, Any]] = {}
        # Hydrate only a small provider-ranked top set. The client's shared lock
        # retains MusicBrainz's global one-request-per-second policy.
        for value in search_rows[:3]:
            release_mbid = str(value.get("id") or "")
            if not release_mbid:
                continue
            try:
                detail = await client.lookup(
                    "release",
                    release_mbid,
                    includes=("artist-credits", "recordings", "release-groups", "media"),
                )
            except MusicBrainzError:
                detail = None
            hydrated[release_mbid] = _release_result(detail or value, include_tracks=True)
        compact = [
            hydrated.get(str(value.get("id") or ""), _release_result(value))
            for value in search_rows
        ]
        candidates = [_release_match_candidate(value) for value in compact]
        ranked = ReleaseMetadataMatcher().rank(
            requested_album=values.album,
            requested_primary_type="Album",
            requested_version=None,
            requested_year=values.year,
            candidates=candidates,
            limit=values.limit,
        )
        by_mbid = {
            str(item.get("release_mbid")): item for item in compact if item.get("release_mbid")
        }
        results: list[dict[str, Any]] = []
        for match in ranked:
            item = dict(by_mbid.get(str(match.candidate.release_mbid), {}))
            item.update(
                {
                    "match_score": round(match.score, 2),
                    "decision": match.decision,
                    "lead": round(match.lead, 2) if match.lead is not None else None,
                    "reasons": list(match.reasons),
                }
            )
            results.append(item)
        return {
            "count": min(int(payload.get("count") or len(rows)), 100_000),
            "association_scope": "review_only_without_recording_and_placement_evidence",
            "hydrated_count": len(hydrated),
            "releases": results,
        }

    registry.register(
        ToolDefinition(
            name="musicbrainz_search_recordings",
            description=(
                "Search and conservatively rank up to 25 canonical recording candidates. "
                "MusicBrainz is primary; configured Apple metadata is a no-result fallback."
            ),
            parameters=strict_json_schema(RecordingSearchArguments.model_json_schema()),
            handler=recordings,
            max_result_bytes=64_000,
            cache_ttl_seconds=86_400,
            cache_vary=_recording_constraint_cache_vary,
        )
    )
    registry.register(
        ToolDefinition(
            name="musicbrainz_search_releases",
            description="Search up to 25 MusicBrainz releases by artist, album, and optional year.",
            parameters=strict_json_schema(ReleaseSearchArguments.model_json_schema()),
            handler=releases,
            max_result_bytes=64_000,
            cache_ttl_seconds=86_400,
        )
    )


def _recording_constraint_cache_vary() -> dict[str, str | None]:
    """Partition cached rankings by trusted request constraints, not model claims."""

    authorization = current_tool_authorization()
    return {
        "requested_album": authorization.requested_album if authorization is not None else None,
        "requested_version": (
            authorization.requested_version if authorization is not None else None
        ),
    }


def _release_result(value: object, *, include_tracks: bool = False) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    release_group = value.get("release-group")
    if not isinstance(release_group, Mapping):
        release_group = {}
    media = value.get("media")
    formats: list[str] = []
    track_count = 0
    tracks: list[dict[str, Any]] = []
    if isinstance(media, list):
        for medium in media[:10]:
            if not isinstance(medium, Mapping):
                continue
            if medium.get("format"):
                formats.append(str(medium["format"])[:100])
            try:
                track_count += max(0, int(medium.get("track-count") or 0))
            except (TypeError, ValueError):
                pass
            if include_tracks:
                medium_tracks = medium.get("tracks")
                if isinstance(medium_tracks, list):
                    for track in medium_tracks:
                        if len(tracks) >= 50 or not isinstance(track, Mapping):
                            break
                        recording = track.get("recording")
                        if not isinstance(recording, Mapping):
                            recording = {}
                        length = recording.get("length") or track.get("length")
                        try:
                            duration = float(length) / 1000 if length else None
                        except (TypeError, ValueError):
                            duration = None
                        tracks.append(
                            {
                                "position": _optional_int(track.get("position")),
                                "number": str(track.get("number") or "")[:20] or None,
                                "title": str(recording.get("title") or track.get("title") or "")[
                                    :300
                                ],
                                "artist_credit": _artist_credit(
                                    recording.get("artist-credit") or track.get("artist-credit")
                                ),
                                "recording_mbid": str(recording.get("id") or "") or None,
                                "duration_seconds": duration,
                            }
                        )
    secondary_types = release_group.get("secondary-types")
    return {
        "release_mbid": str(value.get("id") or "") or None,
        "release_group_mbid": str(release_group.get("id") or "") or None,
        "title": str(value.get("title") or "")[:300],
        "artist_credit": _artist_credit(value.get("artist-credit")),
        "status": str(value.get("status") or "")[:80] or None,
        "date": str(value.get("date") or "")[:20] or None,
        "country": str(value.get("country") or "")[:10] or None,
        "primary_type": str(release_group.get("primary-type") or "")[:80] or None,
        "secondary_types": (
            [str(item)[:80] for item in secondary_types[:10]]
            if isinstance(secondary_types, list)
            else []
        ),
        "formats": formats[:10],
        "track_count": track_count or None,
        "tracks": tracks,
    }


def _release_match_candidate(value: Mapping[str, Any]) -> ReleaseMetadataCandidate:
    date = str(value.get("date") or "")
    return ReleaseMetadataCandidate(
        album=str(value.get("title") or ""),
        status=str(value.get("status") or "") or None,
        primary_type=str(value.get("primary_type") or "") or None,
        # This tool has album intent but no requested recording, duration, or
        # placement. Unknown evidence remains neutral and keeps results review-only.
        recording_fit=0.5,
        version_fit=0.5,
        duration_fit=0.5,
        track_placement_fit=0.5,
        original_year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
        edition=str(value.get("title") or "") or None,
        release_mbid=str(value.get("release_mbid") or "") or None,
    )


def _artist_credit(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "".join(
        f"{item.get('name') or ''!s}{item.get('joinphrase') or ''!s}"
        for item in value
        if isinstance(item, Mapping)
    )[:500]


def _lucene(value: str) -> str:
    return value.strip().replace("\\", "\\\\").replace('"', '\\"')


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
