from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.clients.apple_metadata import AppleMetadataClient, AppleMetadataDisabled
from app.clients.cover_art import CoverArtClient
from app.clients.listenbrainz import ListenBrainzClient
from app.clients.musicbrainz import MusicBrainzClient
from app.clients.openai import OpenAIResponsesClient, music_proposal_format
from app.config import Settings

MBID = "f59c5520-5f46-4d2c-b2c4-822eabf53419"


def settings(**overrides: object) -> Settings:
    return Settings(
        environment="test",
        musicbrainz_user_agent="MusicAgentTests/1.0 (tests@example.test)",
        **overrides,
    )


@pytest.mark.asyncio
async def test_musicbrainz_identifies_and_uses_hyphenated_browse_key(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"releases": []})

    http = httpx.AsyncClient(
        base_url="https://musicbrainz.org/ws/2/", transport=httpx.MockTransport(handler)
    )
    client = MusicBrainzClient(
        settings(database_path=tmp_path / "music-agent.db"),
        http_client=http,
        max_retries=0,
    )
    await client.browse("release", "release-group", MBID, limit=5)

    query = parse_qs(requests[0].url.query.decode())
    assert query["release-group"] == [MBID]
    assert requests[0].headers["user-agent"].startswith("MusicAgentTests/")
    await http.aclose()


@pytest.mark.asyncio
async def test_musicbrainz_rate_limit_is_shared_across_clients(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    clock = [1_000.0]
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"recordings": []})

    async def advance(delay: float) -> None:
        delays.append(delay)
        clock[0] += delay

    shared_settings = settings(database_path=tmp_path / "music-agent.db")
    http_one = httpx.AsyncClient(
        base_url="https://musicbrainz.org/ws/2/", transport=httpx.MockTransport(handler)
    )
    http_two = httpx.AsyncClient(
        base_url="https://musicbrainz.org/ws/2/", transport=httpx.MockTransport(handler)
    )
    first = MusicBrainzClient(
        shared_settings,
        http_client=http_one,
        sleep=advance,
        wall_clock=lambda: clock[0],
        max_retries=0,
    )
    second = MusicBrainzClient(
        shared_settings,
        http_client=http_two,
        sleep=advance,
        wall_clock=lambda: clock[0],
        max_retries=0,
    )

    await first.search_recordings(artist="Artist", title="First")
    await second.search_recordings(artist="Artist", title="Second")

    assert len(requests) == 2
    assert delays == [pytest.approx(1.0)]
    await http_one.aclose()
    await http_two.aclose()


@pytest.mark.asyncio
async def test_cover_art_upgrades_known_legacy_archive_urls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "release": f"https://musicbrainz.org/release/{MBID}",
                "images": [
                    {
                        "id": 7,
                        "front": True,
                        "approved": True,
                        "image": "http://coverartarchive.org/release/example.jpg",
                        "thumbnails": {"500": "http://archive.org/download/a/cover.jpg"},
                    }
                ],
            },
        )

    http = httpx.AsyncClient(
        base_url="https://coverartarchive.org/", transport=httpx.MockTransport(handler)
    )
    client = CoverArtClient(settings(), http_client=http, max_retries=0)
    art = await client.front(release_mbid=MBID)

    assert art is not None
    assert art.image_url.startswith("https://coverartarchive.org/")
    assert art.thumbnail_url.startswith("https://archive.org/")
    await http.aclose()


@pytest.mark.asyncio
async def test_listenbrainz_token_and_recommendation_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"payload": {"mbids": []}})

    http = httpx.AsyncClient(
        base_url="https://api.listenbrainz.org/", transport=httpx.MockTransport(handler)
    )
    client = ListenBrainzClient(
        settings(listenbrainz_token=SecretStr("token-value")),
        http_client=http,
        max_retries=0,
    )
    await client.recommendations("listener", count=10)

    assert requests[0].url.path == "/1/cf/recommendation/user/listener/recording"
    assert requests[0].headers["authorization"] == "Token token-value"
    await http.aclose()


@pytest.mark.asyncio
async def test_apple_fallback_is_opt_in_and_uses_explicit_storefront() -> None:
    disabled = AppleMetadataClient(settings())
    with pytest.raises(AppleMetadataDisabled):
        await disabled.search("track")
    await disabled.aclose()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"resultCount": 0, "results": []})

    http = httpx.AsyncClient(
        base_url="https://itunes.apple.com/", transport=httpx.MockTransport(handler)
    )
    enabled = AppleMetadataClient(
        settings(apple_metadata_enabled=True, apple_storefront="GB"),
        http_client=http,
        max_retries=0,
    )
    await enabled.search("artist track", limit=5)
    query = parse_qs(requests[0].url.query.decode())
    assert query["country"] == ["GB"]
    assert query["media"] == ["music"]
    await http.aclose()


@pytest.mark.asyncio
async def test_openai_adapter_disables_storage_and_parallel_tools() -> None:
    captured: dict[str, object] = {}

    class Responses:
        async def create(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"id": "resp_test", "output": []}

    adapter = OpenAIResponsesClient(
        settings(openai_max_output_tokens=16_000),
        client=SimpleNamespace(responses=Responses()),
    )
    await adapter.create_response(
        input_items="find something",
        instructions="instructions",
        tools=[],
        enable_web_search=True,
        safety_identifier="a" * 64,
        prompt_cache_key="stable-cache-key",
        max_tool_calls=7,
    )

    assert captured["store"] is False
    assert captured["parallel_tool_calls"] is False
    assert captured["safety_identifier"] == "a" * 64
    assert captured["prompt_cache_key"] == "stable-cache-key"
    assert captured["max_tool_calls"] == 7
    assert captured["max_output_tokens"] == 16_000
    assert "reasoning.encrypted_content" in captured["include"]
    assert captured["tools"] == [{"type": "web_search_preview", "search_context_size": "low"}]
    proposal_format = music_proposal_format()
    assert proposal_format["strict"] is True
    assert proposal_format["schema"]["additionalProperties"] is False
    assert set(proposal_format["schema"]["required"]) == {
        "summary",
        "clarification",
        "exhausted",
        "tracks",
    }
