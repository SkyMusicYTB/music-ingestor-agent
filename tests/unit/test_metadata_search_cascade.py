from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.clients.musicbrainz import MusicBrainzClient, MusicBrainzError
from app.config import Settings
from app.services.artist_credits import (
    artist_credit_similarity,
    artist_credit_variant,
    structured_artists,
)
from app.services.metadata_matching import MetadataMatcher, candidates_from_musicbrainz
from app.sources.provider_metadata import resolve_provider_recording_metadata
from app.workers.metadata import (
    MAX_METADATA_SEARCH_REQUESTS,
    MAX_RECORDING_SEARCHES,
    MusicBrainzWorkerResolver,
    WorkerMetadataError,
    _recording_queries,
)

RECORDING_ID = "24f4e1df-a51a-4dc4-a0a3-28f8dd66a011"
RELEASE_ID = "bb7c6979-b9ad-43cf-a264-387ec53a817f"
GROUP_ID = "28738316-cba2-43c6-938b-d156669e0e82"


def recording(**overrides: Any) -> dict[str, Any]:
    return {
        "id": RECORDING_ID,
        "title": "Tarantella",
        "length": 146_000,
        "artist-credit": [{"name": "Gabry Ponte", "joinphrase": " & "}, {"name": "KEL"}],
        "first-release-date": "2024-04-12",
        "releases": [
            {
                "id": RELEASE_ID,
                "title": "Tarantella",
                "status": "Official",
                "date": "2024-04-12",
                "release-group": {"id": GROUP_ID, "primary-type": "Single"},
            }
        ],
        **overrides,
    }


class FakeMusicBrainz:
    def __init__(
        self,
        search: Callable[[str | None, tuple[str, ...]], list[dict[str, Any]]] | None = None,
        *,
        release: dict[str, Any] | None = None,
        via_group: bool = False,
    ) -> None:
        self.search = search or (lambda _artist, _terms: [])
        self.release = release
        self.via_group = via_group
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search_recordings(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("recordings", kwargs))
        return {"recordings": self.search(kwargs["artist"], kwargs.get("artist_terms", ()))}

    async def search_releases(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("releases", kwargs))
        return {"releases": [{"id": RELEASE_ID}] if self.release and not self.via_group else []}

    async def search_release_groups(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("groups", kwargs))
        return {"release-groups": [{"id": GROUP_ID}] if self.release else []}

    async def browse(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("browse", kwargs))
        return {"releases": [{"id": RELEASE_ID}, {"id": RELEASE_ID}]}

    async def lookup(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        self.calls.append(("lookup", kwargs))
        return self.release


async def resolve(fake: FakeMusicBrainz, **kwargs: Any) -> Any:
    resolver = object.__new__(MusicBrainzWorkerResolver)
    resolver._client = cast(MusicBrainzClient, fake)
    resolver._matcher = MetadataMatcher()
    arguments = {
        "artist": "Gabry Ponte, KEL",
        "artists": ("Gabry Ponte", "KEL"),
        "title": "Tarantella",
        "album": None,
        "duration_seconds": 146,
        "version_signature": "studio",
        "year": 2024,
        **kwargs,
    }
    return await resolver._resolve_async(**arguments)


@pytest.mark.parametrize(
    "separator", [", ", " & ", " and ", " x ", " with ", " feat. ", " featuring ", "; "]
)
def test_collaboration_separator_comparison_preserves_full_identity(separator: str) -> None:
    credit = f"Gabry Ponte{separator}KEL"
    artists = ("Gabry Ponte", "KEL")
    assert (
        artist_credit_similarity(
            credit,
            "Gabry Ponte & KEL",
            left_artists=artists,
            right_artists=artists,
        )
        == 1
    )
    assert artist_credit_variant(credit, artists=artists) == "Gabry Ponte & KEL"
    assert artist_credit_similarity(credit, "Gabry Ponte") < 0.9


def test_structured_artists_preserve_punctuation_and_do_not_invent_performers() -> None:
    artists = ("Earth, Wind & Fire", "Florence and the Machine")
    resolved = resolve_provider_recording_metadata(
        {"artists": list(artists), "track": "Test", "uploader": "Unrelated archive"}
    )
    assert resolved.artists == artists
    assert resolved.artist == ", ".join(artists)
    assert resolved.uploader == "Unrelated archive"
    assert resolved.artist_source == "artist"
    assert structured_artists(["KEL", "kel", "", 123, "Bad\nArtist"]) == ("KEL",)
    queries = _recording_queries("Earth, Wind & Fire", ("Earth, Wind & Fire",))
    assert queries == (("Earth, Wind & Fire", ()), (None, ()))
    assert (
        artist_credit_similarity(
            "KEL & Gabry Ponte",
            "Gabry Ponte & KEL",
            left_artists=("KEL", "Gabry Ponte"),
            right_artists=("Gabry Ponte", "KEL"),
        )
        == 1
    )


def test_singleton_artist_punctuation_is_not_collaboration_authority() -> None:
    assert artist_credit_similarity("Tyler, the Creator", "Tyler & the Creator") < 0.95
    assert (
        artist_credit_variant("Earth, Wind & Fire", artists=("Earth, Wind & Fire",))
        == "Earth, Wind & Fire"
    )
    assert (
        artist_credit_similarity(
            "Gabry Ponte, KEL",
            "Gabry Ponte & KEL",
            right_artists=("Gabry Ponte", "KEL"),
        )
        == 1
    )


def test_long_artist_credit_cannot_hide_a_missing_short_collaborator() -> None:
    artist = "The Really Really Really Really Long Named Artist"

    assert artist_credit_similarity(artist, f"{artist} & X") < 0.95
    assert (
        artist_credit_similarity(
            artist,
            f"{artist} & X",
            left_artists=(artist,),
            right_artists=(artist, "X"),
        )
        < 0.95
    )


def test_uploader_equivalent_creator_never_becomes_recording_artist() -> None:
    source = {"creator": "Uploader Name", "uploader": "Uploader Name", "title": "Tarantella"}
    recording_fields = resolve_provider_recording_metadata(source)
    assert recording_fields.artist is None
    assert recording_fields.artist_source is None
    assert recording_fields.uploader == "Uploader Name"
    assert resolve_provider_recording_metadata({**source, "artist": "Gabry Ponte"}).artist == (
        "Gabry Ponte"
    )
    assert (
        resolve_provider_recording_metadata(
            {**source, "title": "Gabry Ponte & KEL - Tarantella"}
        ).artist
        == "Gabry Ponte & KEL"
    )


@pytest.mark.asyncio
async def test_tarantella_credit_variant_finds_confident_official_recording() -> None:
    fake = FakeMusicBrainz(
        lambda artist, _terms: [recording()] if artist == "Gabry Ponte & KEL" else []
    )
    result = await resolve(fake)
    assert result.decision == "auto"
    assert result.reason_code == "matched"
    assert result.candidate.recording_mbid == RECORDING_ID
    assert result.candidate.artist == "Gabry Ponte & KEL"
    assert result.candidate.artists == ("Gabry Ponte", "KEL")
    assert result.candidate.year == 2024
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_exact_result_does_not_run_additional_searches() -> None:
    fake = FakeMusicBrainz(lambda _artist, _terms: [recording()])
    assert (await resolve(fake)).decision == "auto"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_structured_collaborator_search_finds_and_deduplicates_recording() -> None:
    # The identical MBID must not compete with itself and produce a zero lead.
    fake = FakeMusicBrainz(
        lambda artist, _terms: [recording(), recording()] if artist == "KEL" else []
    )
    result = await resolve(fake)
    assert result.decision == "auto"
    assert len(result.options) == 1
    assert [kwargs["artist"] for _, kwargs in fake.calls] == [
        "Gabry Ponte, KEL",
        "Gabry Ponte & KEL",
        "Gabry Ponte",
        "KEL",
    ]


@pytest.mark.asyncio
async def test_repeated_recording_can_gain_a_better_release_during_cascade() -> None:
    compilation = recording(
        releases=[
            {
                "id": "53674b6e-0df0-4631-87d0-83146155e169",
                "title": "Greatest Hits Compilation",
                "date": "2025",
                "status": "Official",
                "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]},
            }
        ]
    )
    fake = FakeMusicBrainz(
        lambda artist, _terms: [compilation] if artist == "Gabry Ponte, KEL" else [recording()]
    )
    result = await resolve(fake)
    assert result.decision == "auto"
    assert len(result.options) == 1
    assert result.candidate.release_mbid == RELEASE_ID
    assert result.candidate.album == "Tarantella"
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"artist-credit": [{"name": "Unrelated Performer"}]},
        {"title": "Tarantella (Live)"},
        {"title": "Tarantella (Cover)"},
        {"title": "Tarantella (Karaoke)"},
        {"title": "Tarantella (Remix)"},
        {"length": None},
        {"length": 240_000},
    ],
)
@pytest.mark.asyncio
async def test_title_only_never_grants_identity_without_artist_duration_and_version(
    overrides: dict[str, Any],
) -> None:
    fake = FakeMusicBrainz(
        lambda artist, terms: [recording(**overrides)] if artist is None and not terms else []
    )
    result = await resolve(fake)
    assert result.decision == "reject"
    assert result.candidate is None
    assert result.reason_code == "low_confidence"


@pytest.mark.asyncio
async def test_title_only_can_find_a_strictly_matching_recording() -> None:
    fake = FakeMusicBrainz(
        lambda artist, terms: [recording()] if artist is None and not terms else []
    )
    assert (await resolve(fake)).decision == "auto"


@pytest.mark.parametrize("via_group", [False, True])
@pytest.mark.asyncio
async def test_release_track_ids_are_bounded_fallback_candidates(via_group: bool) -> None:
    release = {
        "id": RELEASE_ID,
        "title": "Tarantella",
        "date": "2024-04-12",
        "status": "Official",
        "release-group": {"id": GROUP_ID, "primary-type": "Single"},
        "media": [{"tracks": [{"recording": recording()}]}],
    }
    fake = FakeMusicBrainz(release=release, via_group=via_group)
    result = await resolve(fake)
    assert result.decision == "auto"
    assert result.candidate.recording_mbid == RECORDING_ID
    assert len(fake.calls) <= MAX_METADATA_SEARCH_REQUESTS
    assert sum(name == "lookup" for name, _ in fake.calls) == 1


@pytest.mark.asyncio
async def test_empty_search_cascade_is_finite_for_large_artist_arrays() -> None:
    fake = FakeMusicBrainz()
    result = await resolve(fake, artists=tuple(f"Artist {i}" for i in range(30)))
    assert result.reason_code == "no_candidates"
    assert sum(name == "recordings" for name, _ in fake.calls) <= MAX_RECORDING_SEARCHES
    assert len(fake.calls) <= MAX_METADATA_SEARCH_REQUESTS
    for _name, arguments in fake.calls:
        assert arguments["limit"] <= 25


@pytest.mark.asyncio
async def test_musicbrainz_search_keeps_lucene_phrases_escaped(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"recordings": []})

    async with httpx.AsyncClient(
        base_url="https://musicbrainz.org/ws/2/", transport=httpx.MockTransport(handler)
    ) as http:
        client = MusicBrainzClient(
            Settings(environment="test", database_path=tmp_path / "test.db"),
            http_client=http,
            max_retries=0,
        )
        await client.search_recordings(
            artist=None, title='Song" OR *:*', artist_terms=("First", "Second"), limit=10
        )
    assert requests[0].url.params["query"] == (
        'recording:"Song\\" OR *:*" AND artist:"First" AND artist:"Second"'
    )


@pytest.mark.parametrize(
    ("status", "payload", "reason", "retryable"),
    [
        (503, {}, "temporary_failure", True),
        (429, {}, "temporary_failure", True),
        (200, [], "malformed_response", False),
        (403, {}, "rejected_request", False),
    ],
)
@pytest.mark.asyncio
async def test_client_availability_is_not_a_no_candidate_result(
    tmp_path: Path, status: int, payload: object, reason: str, retryable: bool
) -> None:
    async with httpx.AsyncClient(
        base_url="https://musicbrainz.org/ws/2/",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status, json=payload)),
    ) as http:
        client = MusicBrainzClient(
            Settings(environment="test", database_path=tmp_path / "test.db"),
            http_client=http,
            max_retries=0,
        )
        with pytest.raises(MusicBrainzError) as error:
            await client.search_recordings(artist="Artist", title="Title")
    assert error.value.reason_code == reason
    assert error.value.retryable is retryable


def test_worker_preserves_provider_failure_classification(tmp_path: Path) -> None:
    class FailedMusicBrainz(FakeMusicBrainz):
        async def search_recordings(self, **kwargs: Any) -> dict[str, Any]:
            raise MusicBrainzError(
                "invalid shape", reason_code="malformed_response", retryable=False
            )

    resolver = MusicBrainzWorkerResolver(
        Settings(environment="test", database_path=tmp_path / "test.db")
    )
    resolver._client = cast(MusicBrainzClient, FailedMusicBrainz())
    try:
        with pytest.raises(WorkerMetadataError) as error:
            resolver.resolve(
                artist="Artist",
                title="Track",
                album=None,
                duration_seconds=146,
                version_signature="studio",
            )
        assert error.value.reason_code == "malformed_response"
        assert error.value.retryable is False
    finally:
        resolver.close()


def test_malformed_musicbrainz_ids_and_durations_never_become_verified() -> None:
    candidates = candidates_from_musicbrainz(
        {"recordings": [recording(id="made-up"), recording(length="not a number")]}
    )
    assert len(candidates) == 1
    assert candidates[0].recording_mbid == RECORDING_ID
    assert candidates[0].duration_seconds is None
