from __future__ import annotations

import json
from typing import Any

import pytest

from app.tools.listenbrainz import register_listenbrainz_tools
from app.tools.media_sources import media_tool_authorization
from app.tools.musicbrainz import register_musicbrainz_tools
from app.tools.registry import ToolRegistry

RELEASE_MBID = "00000000-0000-4000-8000-000000000001"
RELEASE_GROUP_MBID = "00000000-0000-4000-8000-000000000002"
RECORDING_MBID = "00000000-0000-4000-8000-000000000003"


class FakeMusicBrainz:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    async def search(
        self, entity: str, query: str, *, limit: int = 25, offset: int = 0
    ) -> dict[str, Any]:
        assert entity == "release"
        assert 'release:"Album"' in query
        return {
            "count": 1,
            "releases": [
                {
                    "id": RELEASE_MBID,
                    "title": "Album",
                    "status": "Official",
                    "date": "2000-01-01",
                    "score": 100,
                    "release-group": {
                        "id": RELEASE_GROUP_MBID,
                        "primary-type": "Album",
                    },
                }
            ][:limit],
        }

    async def lookup(self, entity: str, mbid: str, *, includes: tuple[str, ...]) -> dict[str, Any]:
        assert entity == "release"
        assert "recordings" in includes
        self.lookups.append(mbid)
        return {
            "id": RELEASE_MBID,
            "title": "Album",
            "status": "Official",
            "date": "2000-01-01",
            "release-group": {"id": RELEASE_GROUP_MBID, "primary-type": "Album"},
            "media": [
                {
                    "format": "Digital Media",
                    "track-count": 1,
                    "tracks": [
                        {
                            "position": 1,
                            "number": "1",
                            "recording": {
                                "id": RECORDING_MBID,
                                "title": "Song",
                                "length": 200000,
                                "artist-credit": [{"name": "Artist"}],
                            },
                        }
                    ],
                }
            ],
        }

    async def search_recordings(
        self, *, artist: str, title: str, limit: int = 10
    ) -> dict[str, Any]:
        return {"recordings": []}


class FakeApple:
    async def search_tracks(self, *, artist: str, title: str, limit: int = 10) -> dict[str, Any]:
        return {
            "results": [
                {
                    "kind": "song",
                    "artistName": artist,
                    "trackName": title,
                    "collectionName": "Album",
                    "releaseDate": "2000-01-01T00:00:00Z",
                    "trackTimeMillis": 200_000,
                }
            ][:limit]
        }


class VersionedMusicBrainz:
    def __init__(self, special_version: str = "live") -> None:
        self.special_version = special_version
        self.calls = 0

    async def search_recordings(
        self, *, artist: str, title: str, limit: int = 10
    ) -> dict[str, Any]:
        self.calls += 1
        special_label = self.special_version.replace("_", " ").title()

        def recording(
            identifier: str,
            recording_title: str,
            album: str,
            release_identifier: str,
        ) -> dict[str, Any]:
            return {
                "id": identifier,
                "title": recording_title,
                "length": 200_000,
                "artist-credit": [{"name": artist}],
                "releases": [
                    {
                        "id": release_identifier,
                        "title": album,
                        "status": "Official",
                        "date": "2000-01-01",
                        "release-group": {
                            "id": release_identifier.replace("4000", "4001"),
                            "primary-type": "Album",
                        },
                    }
                ],
            }

        return {
            "recordings": [
                recording(
                    "10000000-0000-4000-8000-000000000001",
                    title,
                    "Different Album",
                    "20000000-0000-4000-8000-000000000001",
                ),
                recording(
                    "30000000-0000-4000-8000-000000000001",
                    f"{title} ({special_label})",
                    "Requested Album",
                    "40000000-0000-4000-8000-000000000001",
                ),
            ][:limit]
        }


@pytest.mark.asyncio
async def test_release_tool_uses_matcher_and_bounded_hydrated_tracklist() -> None:
    client = FakeMusicBrainz()
    registry = ToolRegistry()
    register_musicbrainz_tools(registry, client)  # type: ignore[arg-type]

    execution = await registry.execute(
        "musicbrainz_search_releases",
        json.dumps({"artist": "Artist", "album": "Album", "year": 2000, "limit": 25}),
    )
    payload = json.loads(execution.output)["result"]

    assert client.lookups == [RELEASE_MBID]
    assert payload["association_scope"].startswith("review_only")
    assert payload["hydrated_count"] == 1
    assert payload["releases"][0]["decision"] == "review"
    assert payload["releases"][0]["match_score"] == 87.5
    assert payload["releases"][0]["tracks"] == [
        {
            "artist_credit": "Artist",
            "duration_seconds": 200.0,
            "number": "1",
            "position": 1,
            "recording_mbid": RECORDING_MBID,
            "title": "Song",
        }
    ]


@pytest.mark.asyncio
async def test_apple_recording_fallback_is_never_auto_associated() -> None:
    registry = ToolRegistry()
    register_musicbrainz_tools(
        registry,
        FakeMusicBrainz(),  # type: ignore[arg-type]
        FakeApple(),  # type: ignore[arg-type]
    )

    execution = await registry.execute(
        "musicbrainz_search_recordings",
        json.dumps(
            {
                "artist": "Artist",
                "title": "Song",
                "album": "Album",
                "duration_seconds": 200,
                "version": "studio",
                "limit": 25,
            }
        ),
    )
    payload = json.loads(execution.output)["result"]

    assert payload["fallback_provider"] == "apple_search"
    assert payload["matches"][0]["decision"] == "review"
    assert payload["matches"][0]["association_scope"] == "review_only_apple_fallback"
    assert payload["matches"][0]["recording_mbid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["live", "remix", "acoustic"])
async def test_recording_tool_binds_explicit_version_to_trusted_request_context(
    version: str,
) -> None:
    client = VersionedMusicBrainz(version)
    registry = ToolRegistry()
    register_musicbrainz_tools(registry, client)  # type: ignore[arg-type]
    arguments = json.dumps(
        {
            "artist": "Artist",
            "title": "Song",
            "album": None,
            "duration_seconds": 200,
            # A model-supplied value cannot weaken the user's explicit request.
            "version": "studio",
            "limit": 25,
        }
    )

    with media_tool_authorization(
        "user-id",
        "request-id",
        requested_version=version,
    ):
        execution = await registry.execute("musicbrainz_search_recordings", arguments)
    payload = json.loads(execution.output)["result"]

    assert payload["matches"][0]["version"].replace("_", " ") == version.replace("_", " ")
    assert payload["matches"][0]["contradiction_codes"] == []
    assert "explicit_version_mismatch" in payload["matches"][1]["contradiction_codes"]


@pytest.mark.asyncio
async def test_model_version_argument_cannot_claim_explicit_user_authority() -> None:
    client = VersionedMusicBrainz("live")
    registry = ToolRegistry()
    register_musicbrainz_tools(registry, client)  # type: ignore[arg-type]

    execution = await registry.execute(
        "musicbrainz_search_recordings",
        json.dumps(
            {
                "artist": "Artist",
                "title": "Song",
                "album": None,
                "duration_seconds": 200,
                "version": "live",
                "limit": 25,
            }
        ),
    )
    payload = json.loads(execution.output)["result"]

    assert payload["matches"][0]["version"] == "studio"
    assert payload["matches"][0]["contradiction_codes"] == []


@pytest.mark.asyncio
async def test_recording_tool_binds_explicit_album_and_cache_to_trusted_context(
    session_factory,
) -> None:
    client = VersionedMusicBrainz("live")
    registry = ToolRegistry(session_factory)
    register_musicbrainz_tools(registry, client)  # type: ignore[arg-type]
    arguments = json.dumps(
        {
            "artist": "Artist",
            "title": "Song",
            # Deliberately conflict with the trusted user-authored constraint.
            "album": "Different Album",
            "duration_seconds": 200,
            "version": None,
            "limit": 25,
        }
    )

    with media_tool_authorization(
        "user-id",
        "request-id",
        requested_album="Requested Album",
    ):
        first = await registry.execute("musicbrainz_search_recordings", arguments)
        cached = await registry.execute("musicbrainz_search_recordings", arguments)
    first_payload = json.loads(first.output)["result"]

    assert first_payload["matches"][0]["album"] == "Requested Album"
    assert first_payload["matches"][0]["contradiction_codes"] == []
    assert "explicit_album_mismatch" in first_payload["matches"][1]["contradiction_codes"]
    assert cached.summary["cached"] is True
    assert client.calls == 1

    # A different trusted constraint must not reuse the prior ranking even when
    # every model-visible argument is identical.
    with media_tool_authorization(
        "user-id",
        "request-id-2",
        requested_album="Different Album",
    ):
        different = await registry.execute("musicbrainz_search_recordings", arguments)
    assert different.summary["cached"] is False
    assert client.calls == 2


class FakeListenBrainz:
    async def top_recordings_for_artist(self, _artist_mbid: str) -> list[dict[str, Any]]:
        return [{"recording_mbid": f"00000000-0000-4000-8000-{index:012x}"} for index in range(150)]

    async def sitewide_top(
        self,
        _entity: str,
        *,
        range_name: str,
        count: int,
        offset: int,
    ) -> dict[str, Any]:
        return {
            "payload": {
                "range": range_name,
                "recordings": [
                    {
                        "recording_mbid": f"00000000-0000-4000-8001-{index:012x}",
                        "track_name": f"Track {index}",
                        "artist_name": "Artist",
                    }
                    for index in range(count + 50)
                ],
            }
        }

    async def artist_radio(
        self,
        _artist_mbid: str,
        *,
        mode: str,
        max_similar_artists: int,
        max_recordings_per_artist: int,
        pop_begin: int,
        pop_end: int,
    ) -> dict[str, Any]:
        return {
            f"artist-{artist}": [
                {
                    "recording_mbid": f"00000000-0000-4000-8002-{artist * 10 + item:012x}",
                    "similar_artist_mbid": f"00000000-0000-4000-8003-{artist:012x}",
                    "similar_artist_name": f"Artist {artist}",
                }
                for item in range(max_recordings_per_artist + 2)
            ]
            for artist in range(max_similar_artists + 2)
        }

    async def recommendations(self, _username: str, *, count: int, offset: int) -> dict[str, Any]:
        return {
            "payload": {
                "user_name": "listener",
                "mbids": [
                    {
                        "recording_mbid": f"00000000-0000-4000-8004-{index:012x}",
                        "score": 100 - index,
                    }
                    for index in range(count + 50)
                ],
            }
        }


@pytest.mark.asyncio
async def test_listenbrainz_popular_tool_preserves_100_item_contract() -> None:
    registry = ToolRegistry()
    register_listenbrainz_tools(
        registry,
        FakeListenBrainz(),  # type: ignore[arg-type]
        default_username=None,
    )

    execution = await registry.execute(
        "listenbrainz_popular_recordings",
        json.dumps(
            {
                "artist_mbid": RELEASE_MBID,
                "period": "all_time",
                "limit": 100,
            }
        ),
    )
    payload = json.loads(execution.output)["result"]

    assert execution.status == "completed"
    assert len(payload["items"]) == 100


@pytest.mark.asyncio
async def test_listenbrainz_results_are_compact_and_enforce_argument_caps() -> None:
    registry = ToolRegistry()
    register_listenbrainz_tools(
        registry,
        FakeListenBrainz(),  # type: ignore[arg-type]
        default_username="listener",
    )

    sitewide = await registry.execute(
        "listenbrainz_popular_recordings",
        json.dumps({"artist_mbid": None, "period": "month", "limit": 100}),
    )
    radio = await registry.execute(
        "listenbrainz_artist_radio",
        json.dumps(
            {
                "artist_mbid": RELEASE_MBID,
                "mode": "medium",
                "max_similar_artists": 2,
                "max_recordings_per_artist": 2,
            }
        ),
    )
    recommendations = await registry.execute(
        "listenbrainz_user_recommendations",
        json.dumps({"count": 100}),
    )
    sitewide_payload = json.loads(sitewide.output)["result"]
    radio_payload = json.loads(radio.output)["result"]
    recommendation_payload = json.loads(recommendations.output)["result"]

    assert len(sitewide_payload["items"]) == 100
    assert set(sitewide_payload["items"][0]) <= {
        "recording_mbid",
        "track_name",
        "artist_name",
        "artist_mbids",
        "release_mbid",
        "release_name",
        "listen_count",
    }
    assert radio_payload["artist_count"] == 2
    assert len(radio_payload["items"]) == 4
    assert len(recommendation_payload["items"]) == 100
