from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.clients.ytdlp import DownloadCancelled, SourceValidationError, YtDlpError
from app.db.models import (
    Conversation,
    Event,
    EvidenceReference,
    Request,
    RequestTrack,
    ServiceTask,
    User,
)
from app.db.models import (
    SourceCandidate as DbSourceCandidate,
)
from app.services.source_selection import SourceCandidate
from app.sources import ProviderIdentity, UploaderRelationship
from app.tools.media_sources import (
    ProbeMediaSourceArguments,
    SearchMediaSourcesArguments,
    build_media_source_tools,
    media_tool_authorization,
)
from app.tools.youtube import YouTubeSearchArguments, YouTubeSearchResponse
from app.workers.queue import ServiceTaskLease, ServiceTaskQueue
from app.workers.service_tasks import WorkerServiceTaskHandler, _uploader_relationship


class _FakeYtDlp:
    def validate_url(self, value: str) -> str:
        return value

    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        return {
            "id": "dQw4w9WgXcQ",
            "extractor": "youtube",
            "track": "Resolved Song",
            "artist": "Resolved Artist",
            "album": "Resolved Album",
            "duration": 180,
        }


class _ThirdPartyYouTubeYtDlp(_FakeYtDlp):
    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        return {
            "id": "dQw4w9WgXcQ",
            "extractor": "youtube",
            "title": "Coldplay - Yellow",
            "creator": "Unrelated Fan Archive",
            "uploader": "Unrelated Fan Archive",
            "duration": 266,
        }


class _RejectingYtDlp(_FakeYtDlp):
    def validate_url(self, value: str) -> str:
        raise SourceValidationError(f"rejected source: {value[:8]}")


class _UnavailableYtDlp(_FakeYtDlp):
    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        raise YtDlpError("temporary provider failure")


class _CancelledYtDlp(_FakeYtDlp):
    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        assert cancel_signal is not None and cancel_signal.is_set()
        raise DownloadCancelled("worker shutdown cancelled yt-dlp")


class _SoundCloudYtDlp(_FakeYtDlp):
    def search_provider(self, query: str, *, provider, limit: int, cancel_signal=None):
        assert query
        assert str(provider) == "soundcloud"
        assert limit <= 10
        return {
            "entries": [
                {
                    "id": "123456789",
                    "extractor": "soundcloud",
                    "title": "Resolved Artist - Resolved Song",
                    "uploader": "Unrelated Archive",
                    "duration": 180,
                    "webpage_url": "https://soundcloud.com/archive/resolved-song",
                }
            ]
        }

    def validate_url(self, value: str) -> str:
        assert value.startswith("https://soundcloud.com/")
        return value

    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        return {
            "id": "123456789",
            "extractor": "soundcloud",
            "track": "Resolved Song",
            "artist": "Resolved Artist",
            "uploader": "Unrelated Archive",
            "duration": 180,
        }


class _BandcampYtDlp(_FakeYtDlp):
    def validate_url(self, value: str) -> str:
        assert ".bandcamp.com/track/" in value
        return value

    def probe(self, _value: str, *, cancel_signal=None) -> dict[str, object]:
        return {
            "id": "resolved-song",
            "extractor": "bandcamp",
            "track": "Resolved Song",
            "artist": "Resolved Artist",
            "uploader": "Resolved Artist",
            "duration": 180,
        }


class _BandcampCollectionYtDlp(_BandcampYtDlp):
    def __init__(self) -> None:
        self.limits: list[int] = []

    def inspect_collection(self, _value: str, *, limit: int, cancel_signal=None):
        self.limits.append(limit)
        return {
            "extractor": "bandcamp:album",
            "title": "Resolved Album",
            "artist": "Resolved Artist",
            "entries": [
                {
                    "id": "first-song",
                    "extractor": "bandcamp:track",
                    "title": "Resolved Artist - First Song",
                    "artist": "Resolved Artist",
                    "uploader": "Resolved Artist",
                    "duration": 181,
                    "webpage_url": "https://resolved-artist.bandcamp.com/track/first-song",
                },
                {
                    "id": "second-song",
                    "extractor": "bandcamp:track",
                    "title": "Resolved Artist - Second Song",
                    "artist": "Resolved Artist",
                    "uploader": "Resolved Artist",
                    "duration": 182,
                    "webpage_url": "https://resolved-artist.bandcamp.com/track/second-song",
                },
            ],
        }


class _FakeYouTube:
    def search(self, query: str, *, limit: int, cancel_signal=None):
        return YouTubeSearchResponse(
            query=query,
            candidates=(
                SourceCandidate(
                    source_id="dQw4w9WgXcQ",
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    title="Resolved Artist - Resolved Song",
                    channel="Resolved Artist - Topic",
                    duration_seconds=180,
                ),
            )[:limit],
        )


class _UnusedScanner:
    def run(self, *, full: bool, cancel_signal=None, service_task_id=None):
        raise AssertionError(f"unexpected scan full={full}")


class _CompletedScanner:
    def __init__(self, music_root: Path) -> None:
        self.music_root = music_root

    def run(self, *, full: bool, cancel_signal=None, service_task_id=None):
        assert not full
        return SimpleNamespace(
            id="scan-id",
            kind="incremental",
            status="completed",
            scanned_files=1,
            changed_files=1,
            error_count=0,
        )


class _RecordingDownloadQueue:
    def __init__(self) -> None:
        self.roots: list[Path] = []

    def adopt_published_jobs(self, music_root: Path) -> int:
        self.roots.append(music_root)
        return 1


def _request(session_factory, *, suffix: str) -> str:
    with session_factory.begin() as session:
        user = User(
            username=f"worker-{suffix}",
            username_normalized=f"worker-{suffix}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="direct")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="https://youtu.be/dQw4w9WgXcQ",
            action="add",
            input_kind="youtube_url",
            idempotency_key=f"worker-task-{suffix}",
        )
        session.add(request)
        session.flush()
        return request.id


def _media_scope(session_factory, request_id: str):
    with session_factory() as session:
        request = session.get(Request, request_id)
        assert request is not None
        user_id = request.user_id
    return media_tool_authorization(user_id, request_id)


def _handler(session_factory, queue: ServiceTaskQueue) -> WorkerServiceTaskHandler:
    return WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_FakeYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )


async def _wait_for_service_lease(queue: ServiceTaskQueue) -> ServiceTaskLease:
    for _attempt in range(100):
        lease = await asyncio.to_thread(queue.claim_next)
        if lease is not None:
            return lease
        await asyncio.sleep(0.01)
    raise AssertionError("worker broker task was not enqueued")


def test_direct_request_task_is_resolved_without_arbitrary_dispatch(session_factory) -> None:
    request_id = _request(session_factory, suffix="direct")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    outcome = _handler(session_factory, queue).process(lease)
    assert outcome.completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        track = session.scalar(select(RequestTrack).where(RequestTrack.request_id == request_id))
        confirmation = session.scalar(
            select(ServiceTask).where(
                ServiceTask.target == "web",
                ServiceTask.kind == "confirm_request",
            )
        )
        assert request is not None and request.status == "preview"
        assert track is not None
        assert track.artist == "Resolved Artist"
        assert track.source_id == "dQw4w9WgXcQ"
        assert confirmation is not None and confirmation.state == "queued"
        assert json.loads(confirmation.payload_json) == {"request_id": request_id}


def test_direct_url_does_not_copy_third_party_creator_into_canonical_artist(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="third-party-direct")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_ThirdPartyYouTubeYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )

    assert handler.process(lease).completed
    with session_factory() as session:
        track = session.scalar(select(RequestTrack).where(RequestTrack.request_id == request_id))
        assert track is not None
        candidate = session.scalar(
            select(DbSourceCandidate).where(DbSourceCandidate.request_track_id == track.id)
        )
        assert (track.artist, track.title) == ("Coldplay", "Yellow")
        assert candidate is not None
        assert candidate.provider_artist == "Coldplay"
        assert candidate.uploader == "Unrelated Fan Archive"
        assert candidate.uploader_relationship == "third_party"


def test_direct_collection_creates_bounded_selectable_per_track_candidates(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="direct-collection")
    collection_url = "https://resolved-artist.bandcamp.com/album/resolved-album"
    with session_factory.begin() as session:
        request = session.get(Request, request_id)
        assert request is not None
        request.raw_text = collection_url
        request.input_kind = "media_url"
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    ytdlp = _BandcampCollectionYtDlp()
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=ytdlp,  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
        max_direct_playlist_items=2,
    )

    assert handler.process(lease).completed
    assert ytdlp.limits == [2]
    with session_factory() as session:
        request = session.get(Request, request_id)
        tracks = list(
            session.scalars(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
            )
        )
        candidates = list(
            session.scalars(
                select(DbSourceCandidate)
                .where(DbSourceCandidate.request_track_id.in_([track.id for track in tracks]))
                .order_by(DbSourceCandidate.source_id)
            )
        )
        assert request is not None and request.status == "preview"
        assert request.input_kind == "media_collection_url"
        assert request.requested_count is None
        assert request.discovered_count == 2
        assert [(track.artist, track.title) for track in tracks] == [
            ("Resolved Artist", "First Song"),
            ("Resolved Artist", "Second Song"),
        ]
        assert {candidate.source_id for candidate in candidates} == {
            "first-song",
            "second-song",
        }
        assert all(candidate.probe_status == "pending" for candidate in candidates)
        assert all(candidate.acquisition_url != collection_url for candidate in candidates)


def test_structured_artist_arrays_survive_flat_search_and_evidence_persistence(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="structured-search")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    handler = _handler(session_factory, queue)
    artists = ["Gabry Ponte", "KEL"]
    parsed = handler._flat_provider_candidate(
        ProviderIdentity.YOUTUBE,
        {
            "id": "rxw1RCAY3qw",
            "extractor": "youtube",
            "artists": artists,
            "title": "Tarantella",
            "duration": 146,
            "uploader": "Unrelated Archive",
        },
    )
    assert parsed is not None
    assert parsed["artists"] == artists
    handler._persist_media_evidence(
        request_id=request_id,
        request_track_id=None,
        provider=ProviderIdentity.YOUTUBE,
        candidates=[parsed],
        limit=1,
    )
    with session_factory() as session:
        evidence = session.scalar(
            select(EvidenceReference).where(EvidenceReference.request_id == request_id)
        )
        assert evidence is not None
        candidate = session.scalar(
            select(DbSourceCandidate).where(DbSourceCandidate.evidence_id == evidence.id)
        )
        assert candidate is not None
        assert json.loads(evidence.sanitized_metadata_json)["artists"] == artists
        assert json.loads(candidate.sanitized_metadata_json)["artists"] == artists


@pytest.mark.parametrize("entry_has_artists", [True, False])
def test_structured_artist_arrays_survive_direct_collection(
    session_factory, entry_has_artists: bool
) -> None:
    request_id = _request(session_factory, suffix=f"structured-collection-{entry_has_artists}")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    handler = _handler(session_factory, queue)
    # A punctuation-bearing band remains one artist, including when a track
    # inherits a structured album credit instead of providing its own.
    artists = ["Earth, Wind & Fire", "KEL"]
    entry = {
        "id": "song",
        "extractor": "bandcamp",
        "title": "Song",
        "duration": 146,
        "webpage_url": "https://fixture.bandcamp.com/track/song",
    }
    if entry_has_artists:
        entry["artists"] = artists
    handler._store_direct_collection(
        request_id,
        "https://fixture.bandcamp.com/album/album",
        {"extractor": "bandcamp:album", "title": "Album", "artists": artists, "entries": [entry]},
    )
    with session_factory() as session:
        track = session.scalar(select(RequestTrack).where(RequestTrack.request_id == request_id))
        assert track is not None
        evidence = session.scalar(
            select(EvidenceReference).where(EvidenceReference.request_track_id == track.id)
        )
        assert evidence is not None
        candidate = session.scalar(
            select(DbSourceCandidate).where(DbSourceCandidate.evidence_id == evidence.id)
        )
        assert candidate is not None
        assert json.loads(track.metadata_provenance_json)["artists"] == artists
        assert json.loads(evidence.sanitized_metadata_json)["artists"] == artists
        assert json.loads(candidate.sanitized_metadata_json)["artists"] == artists


def test_youtube_search_task_is_bounded_and_serialized(session_factory) -> None:
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="youtube_search",
            payload_json=json.dumps({"query": "Resolved Artist Song", "limit": 8}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    assert _handler(session_factory, queue).process(lease).completed
    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        assert task is not None and task.state == "completed"
        result = json.loads(task.result_json)
        assert result["candidates"][0]["source_id"] == "dQw4w9WgXcQ"


def test_youtube_search_tool_schema_requires_all_arguments() -> None:
    schema = YouTubeSearchArguments.model_json_schema()
    assert set(schema["required"]) == {"query", "limit"}


def test_finite_media_search_persists_urls_only_in_worker_database(session_factory) -> None:
    request_id = _request(session_factory, suffix="finite-search")
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="search_media_sources",
            payload_json=json.dumps({"intent_id": request_id, "provider": "youtube", "limit": 3}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id

    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    assert _handler(session_factory, queue).process(lease).completed

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        assert task is not None
        result = json.loads(task.result_json or "{}")
        serialized = json.dumps(result, sort_keys=True)
        assert "https://" not in serialized
        assert "url" not in result["candidates"][0]
        evidence_id = result["candidates"][0]["evidence_id"]
        evidence = session.get(EvidenceReference, evidence_id)
        candidate = session.scalar(
            select(DbSourceCandidate).where(DbSourceCandidate.evidence_id == evidence_id)
        )
        assert evidence is not None
        assert evidence.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert candidate is not None and candidate.acquisition_url == evidence.canonical_url
        assert candidate.probe_status == "pending"


def test_finite_media_probe_returns_opaque_id_without_worker_url(session_factory) -> None:
    request_id = _request(session_factory, suffix="finite-probe")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="search_media_sources",
                payload_json=json.dumps(
                    {"intent_id": request_id, "provider": "youtube", "limit": 1}
                ),
                available_at=datetime.now(UTC),
            )
        )
    search_lease = queue.claim_next()
    assert search_lease is not None
    assert _handler(session_factory, queue).process(search_lease).completed
    with session_factory() as session:
        search_task = session.get(ServiceTask, search_lease.task_id)
        evidence_id = json.loads(search_task.result_json or "{}")["candidates"][0]["evidence_id"]
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="probe_media_source",
            payload_json=json.dumps({"evidence_id": evidence_id}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id

    probe_lease = queue.claim_next()
    assert probe_lease is not None
    assert _handler(session_factory, queue).process(probe_lease).completed
    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        result = json.loads(task.result_json or "{}")
        assert "url" not in json.dumps(result, sort_keys=True)
        candidate = session.get(DbSourceCandidate, result["source_candidate_id"])
        assert candidate is not None
        assert candidate.policy_status == "allowed"
        assert candidate.probe_status == "valid"
        assert candidate.acquisition_url is not None


def test_media_tool_schemas_expose_only_finite_ids_and_provider() -> None:
    search_schema = SearchMediaSourcesArguments.model_json_schema()
    probe_schema = ProbeMediaSourceArguments.model_json_schema()
    assert set(search_schema["required"]) == {"intent_id", "provider", "limit"}
    assert "query" not in search_schema["properties"]
    assert search_schema["properties"]["provider"]["enum"] == [
        "youtube",
        "soundcloud",
        "bandcamp",
    ]
    assert search_schema["properties"]["limit"]["maximum"] == 10
    assert set(probe_schema["required"]) == {"evidence_id"}


def test_media_tool_builder_replaces_legacy_youtube_tool(session_factory) -> None:
    tools = build_media_source_tools(session_factory, broker_timeout_seconds=1)
    assert {tool.name for tool in tools} == {
        "search_media_sources",
        "probe_media_source",
    }
    assert all("url" not in json.dumps(tool.parameters).casefold() for tool in tools)


@pytest.mark.asyncio
async def test_media_tool_builder_hides_and_rejects_disabled_provider(session_factory) -> None:
    request_id = _request(session_factory, suffix="disabled-provider")
    search_tool, _probe_tool = build_media_source_tools(
        session_factory,
        broker_timeout_seconds=1,
        enabled_providers=["youtube"],
    )
    assert search_tool.parameters["properties"]["provider"]["enum"] == ["youtube"]
    with pytest.raises(ValueError, match="disabled"):
        await search_tool.handler({"intent_id": request_id, "provider": "soundcloud", "limit": 1})


def test_worker_rejects_disabled_provider_before_search(session_factory) -> None:
    request_id = _request(session_factory, suffix="worker-disabled-provider")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_SoundCloudYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
        enabled_providers=["youtube"],
    )
    with pytest.raises(ValueError, match="disabled"):
        handler._search_media_sources(
            {"intent_id": request_id, "provider": "soundcloud", "limit": 1}
        )


@pytest.mark.asyncio
async def test_web_broker_returns_only_sanitized_worker_result(session_factory) -> None:
    request_id = _request(session_factory, suffix="broker-redaction")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    search_tool, probe_tool = build_media_source_tools(
        session_factory,
        broker_timeout_seconds=3,
    )

    with _media_scope(session_factory, request_id):
        search_future = asyncio.create_task(
            search_tool.handler({"intent_id": request_id, "provider": "youtube", "limit": 1})
        )
        search_lease = await _wait_for_service_lease(queue)
        search_outcome = await asyncio.to_thread(
            _handler(session_factory, queue).process,
            search_lease,
        )
        assert search_outcome.completed
        search_result = await search_future
        assert "url" not in json.dumps(search_result, sort_keys=True)
        evidence_id = search_result["candidates"][0]["evidence_id"]

        probe_future = asyncio.create_task(probe_tool.handler({"evidence_id": evidence_id}))
        probe_lease = await _wait_for_service_lease(queue)
        probe_outcome = await asyncio.to_thread(
            _handler(session_factory, queue).process,
            probe_lease,
        )
        assert probe_outcome.completed
        probe_result = await probe_future
    assert "url" not in json.dumps(probe_result, sort_keys=True)
    assert probe_result["source_candidate_id"] != "dQw4w9WgXcQ"


def test_soundcloud_search_is_curated_and_keeps_uploader_separate(session_factory) -> None:
    request_id = _request(session_factory, suffix="soundcloud-search")
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="search_media_sources",
            payload_json=json.dumps(
                {"intent_id": request_id, "provider": "soundcloud", "limit": 2}
            ),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_SoundCloudYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    assert handler.process(lease).completed
    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        result = json.loads(task.result_json or "{}")
        assert result["candidates"][0]["uploader"] == "Unrelated Archive"
        assert "url" not in json.dumps(result)
        evidence = session.get(EvidenceReference, result["candidates"][0]["evidence_id"])
        assert evidence is not None
        assert evidence.canonical_url == "https://soundcloud.com/archive/resolved-song"


def test_evidence_cache_never_crosses_sibling_track_scope(session_factory) -> None:
    request_id = _request(session_factory, suffix="track-scope")
    with session_factory.begin() as session:
        first = RequestTrack(
            request_id=request_id,
            ordinal=1,
            artist="First Artist",
            title="First Song",
            selected=True,
        )
        second = RequestTrack(
            request_id=request_id,
            ordinal=2,
            artist="Second Artist",
            title="Second Song",
            selected=True,
        )
        session.add_all([first, second])
        session.flush()
        for track_id, source_id, title in (
            (first.id, "first-source", "First Song"),
            (second.id, "second-source", "Second Song"),
            (None, "unbound-source", "Request-level Song"),
        ):
            session.add(
                EvidenceReference(
                    request_id=request_id,
                    request_track_id=track_id,
                    provider="bandcamp",
                    evidence_kind="provider_search_result",
                    canonical_url=f"https://artist.bandcamp.com/track/{source_id}",
                    provider_item_id=source_id,
                    status="available",
                    sanitized_metadata_json=json.dumps({"title": title}),
                )
            )
        session.add(
            EvidenceReference(
                request_id=request_id,
                request_track_id=second.id,
                provider="bandcamp",
                evidence_kind="model_evidence",
                canonical_url="https://artist.bandcamp.com/track/model-source",
                provider_item_id="model-source",
                status="available",
                sanitized_metadata_json=json.dumps({"title": "Model Song"}),
            )
        )
        second_id = second.id

    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    rows = _handler(session_factory, queue)._existing_media_evidence(
        request_id=request_id,
        request_track_id=second_id,
        provider=ProviderIdentity.BANDCAMP,
        limit=10,
    )
    assert {row["title"] for row in rows} == {"Second Song", "Request-level Song"}


@pytest.mark.asyncio
async def test_model_evidence_id_is_not_probeable_by_finite_broker(session_factory) -> None:
    request_id = _request(session_factory, suffix="model-evidence")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=request_id,
            provider="youtube",
            evidence_kind="model_evidence",
            canonical_url="https://www.youtube.com/watch?v=model-source",
            provider_item_id="model-source",
            status="available",
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id

    _search_tool, probe_tool = build_media_source_tools(session_factory, broker_timeout_seconds=1)
    with _media_scope(session_factory, request_id):
        with pytest.raises(ValueError, match="unknown, unavailable, or expired"):
            await probe_tool.handler({"evidence_id": evidence_id})
    with session_factory() as session:
        assert session.scalar(select(ServiceTask.id)) is None


@pytest.mark.asyncio
async def test_finite_media_tools_cannot_use_foreign_request_or_evidence_ids(
    session_factory,
) -> None:
    authorized_request = _request(session_factory, suffix="authorized-scope")
    foreign_request = _request(session_factory, suffix="foreign-scope")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=foreign_request,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=foreign-source",
            provider_item_id="foreign-source",
            status="available",
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
    search_tool, probe_tool = build_media_source_tools(session_factory, broker_timeout_seconds=1)

    with _media_scope(session_factory, authorized_request):
        with pytest.raises(ValueError, match="active local request"):
            await search_tool.handler(
                {"intent_id": foreign_request, "provider": "youtube", "limit": 1}
            )
        with pytest.raises(ValueError, match="unknown, unavailable, or expired"):
            await probe_tool.handler({"evidence_id": evidence_id})

    with session_factory() as session:
        assert session.scalar(select(ServiceTask.id)) is None


def test_transient_probe_failure_keeps_candidate_available_for_task_retry(session_factory) -> None:
    request_id = _request(session_factory, suffix="transient-probe")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=request_id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=retry-source",
            provider_item_id="retry-source",
            status="pending",
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
        session.add(
            ServiceTask(
                target="worker",
                kind="probe_media_source",
                payload_json=json.dumps({"evidence_id": evidence_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_UnavailableYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )

    assert not handler.process(lease).completed
    with session_factory() as session:
        evidence = session.get(EvidenceReference, evidence_id)
        task = session.get(ServiceTask, lease.task_id)
        assert evidence is not None and evidence.status == "pending"
        assert evidence.negative_reason is None
        assert task is not None and task.state == "retry_wait"


def test_untrusted_description_and_label_like_name_do_not_gain_match_authority(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="untrusted-provider-text")
    with session_factory.begin() as session:
        track = RequestTrack(
            request_id=request_id,
            ordinal=1,
            artist="Resolved Artist",
            title="Resolved Song",
            selected=True,
        )
        session.add(track)
        session.flush()
        track_id = track.id

    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    handler = _handler(session_factory, queue)
    results = handler._persist_media_evidence(
        request_id=request_id,
        request_track_id=track_id,
        provider=ProviderIdentity.SOUNDCLOUD,
        candidates=[
            {
                "source_id": "description-source",
                "extractor": "soundcloud",
                "url": "https://soundcloud.com/archive/description-source",
                "title": "Resolved Artist - Resolved Song",
                "provider_artist": "Resolved Artist",
                "uploader": "Definitely Official Records Label VEVO",
                "duration_seconds": 180.0,
                "description": "LIVE REMIX KARAOKE cover version",
            }
        ],
        limit=1,
    )
    with session_factory() as session:
        candidate = session.scalar(
            select(DbSourceCandidate).where(
                DbSourceCandidate.evidence_id == results[0]["evidence_id"]
            )
        )
        assert candidate is not None and candidate.version_signature == "studio"
    assert (
        _uploader_relationship(
            "Resolved Artist",
            "Definitely Official Records Label VEVO",
            {"description": "ordinary provider text"},
        )
        is UploaderRelationship.THIRD_PARTY
    )


def test_bandcamp_direct_evidence_can_be_searched_then_probed(session_factory) -> None:
    request_id = _request(session_factory, suffix="bandcamp-evidence")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=request_id,
            provider="bandcamp",
            evidence_kind="direct_user_url",
            canonical_url="https://resolved-artist.bandcamp.com/track/resolved-song",
            provider_item_id="resolved-song",
            status="pending",
            sanitized_metadata_json=json.dumps(
                {
                    "title": "Resolved Song",
                    "uploader": "Resolved Artist",
                    "duration_seconds": 180,
                }
            ),
        )
        session.add(evidence)
        session.flush()
        evidence_id = evidence.id
        session.add(
            ServiceTask(
                target="worker",
                kind="search_media_sources",
                payload_json=json.dumps(
                    {"intent_id": request_id, "provider": "bandcamp", "limit": 2}
                ),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_BandcampYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    search_lease = queue.claim_next()
    assert search_lease is not None and handler.process(search_lease).completed
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="probe_media_source",
                payload_json=json.dumps({"evidence_id": evidence_id}),
                available_at=datetime.now(UTC),
            )
        )
    probe_lease = queue.claim_next()
    assert probe_lease is not None and handler.process(probe_lease).completed
    with session_factory() as session:
        probe_task = session.get(ServiceTask, probe_lease.task_id)
        result = json.loads(probe_task.result_json or "{}")
        assert result["provider"] == "bandcamp"
        assert "url" not in json.dumps(result)
        candidate = session.get(DbSourceCandidate, result["source_candidate_id"])
        assert candidate is not None and candidate.acquisition_url is not None


def test_periodic_library_scan_queue_is_idempotent(session_factory) -> None:
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    first = queue.ensure_scheduled_library_scan()
    second = queue.ensure_scheduled_library_scan()
    assert second == first
    with session_factory() as session:
        task = session.get(ServiceTask, first)
        assert task is not None
        assert task.kind == "library_scan"
        assert task.state == "queued"
        assert json.loads(task.payload_json) == {"full": False, "scheduled": True}


def test_completed_library_scan_reconciles_published_jobs(session_factory, tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    downloads = _RecordingDownloadQueue()
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="library_scan",
            payload_json=json.dumps({"full": False}),
            available_at=datetime.now(UTC),
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_FakeYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_CompletedScanner(music_root),  # type: ignore[arg-type]
        max_duration_seconds=600,
        download_queue=downloads,  # type: ignore[arg-type]
    )

    assert handler.process(lease).completed

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        assert task is not None
        assert json.loads(task.result_json)["reconciled_jobs"] == 1
    assert downloads.roots == [music_root]


def test_terminal_direct_validation_failure_marks_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="invalid")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_RejectingYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    assert not handler.process(lease).completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        event = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert request is not None and request.status == "failed"
        assert request.error_code == "invalid_source_url"
        assert event is not None


def test_exhausted_direct_probe_retries_mark_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="exhausted")
    with session_factory.begin() as session:
        session.add(
            ServiceTask(
                target="worker",
                kind="resolve_direct_request",
                payload_json=json.dumps({"request_id": request_id}),
                available_at=datetime.now(UTC),
                attempts=3,
            )
        )
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None and lease.attempts == 4
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_UnavailableYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
    )
    assert not handler.process(lease).completed
    with session_factory() as session:
        request = session.get(Request, request_id)
        assert request is not None and request.status == "failed"
        assert request.error_code == "source_resolution_failed"


def test_planned_shutdown_releases_direct_task_without_attempt_or_request_failure(
    session_factory,
) -> None:
    request_id = _request(session_factory, suffix="shutdown")
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="resolve_direct_request",
            payload_json=json.dumps({"request_id": request_id}),
            available_at=datetime.now(UTC),
            attempts=3,
        )
        session.add(task)
        session.flush()
        task_id = task.id
    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    lease = queue.claim_next()
    assert lease is not None and lease.attempts == 4
    shutdown = threading.Event()
    shutdown.set()
    handler = WorkerServiceTaskHandler(
        queue=queue,
        factory=session_factory,
        ytdlp=_CancelledYtDlp(),  # type: ignore[arg-type]
        youtube=_FakeYouTube(),  # type: ignore[arg-type]
        scanner=_UnusedScanner(),  # type: ignore[arg-type]
        max_duration_seconds=600,
        shutdown_signal=shutdown,
    )

    assert not handler.process(lease).completed

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        request = session.get(Request, request_id)
        failure = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert task is not None and task.state == "retry_wait"
        assert task.attempts == 3 and task.lease_token is None
        assert request is not None and request.status == "pending"
        assert failure is None


def test_expired_final_direct_task_marks_request_failed(session_factory) -> None:
    request_id = _request(session_factory, suffix="expired")
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        task = ServiceTask(
            target="worker",
            kind="resolve_direct_request",
            payload_json=json.dumps({"request_id": request_id}),
            state="running",
            attempts=4,
            lease_token="expired-token",  # noqa: S106 - inert fencing-token fixture
            lease_expires_at=now - timedelta(seconds=1),
        )
        session.add(task)
        session.flush()
        task_id = task.id

    queue = ServiceTaskQueue(session_factory, target="worker", lease_seconds=30)
    assert queue.recover_expired(now=now) == 1

    with session_factory() as session:
        task = session.get(ServiceTask, task_id)
        request = session.get(Request, request_id)
        event = session.scalar(
            select(Event).where(
                Event.entity_id == request_id,
                Event.event_type == "request.direct_failed",
            )
        )
        assert task is not None and task.state == "failed"
        assert request is not None and request.status == "failed"
        assert request.error_code == "direct_resolution_failed"
        assert event is not None
