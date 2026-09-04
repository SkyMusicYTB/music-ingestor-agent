"""Offline request-to-proposal contracts for exact recording intent."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from app.db.models import (
    Conversation,
    DownloadJob,
    JobDecision,
    Request,
    RequestTrack,
    ServiceTask,
    SourceCandidate,
    User,
)
from app.repositories.jobs import JobRepository
from app.schemas import MusicProposal
from app.services.confirmation import confirmation_decision
from app.services.orchestration import OrchestrationService
from app.services.proposals import ProposalService, VerifiedMetadata
from app.sources import ProviderIdentity
from app.tools.musicbrainz import register_musicbrainz_tools
from app.tools.registry import ToolRegistry
from app.workers.queue import DownloadJobQueue
from app.workers.source_resolution import WorkerSourceResolver


def _response(output: list[dict[str, object]], output_text: str = "") -> dict[str, object]:
    return {
        "id": "resp_exact_track_fixture",
        "service_tier": "default",
        "output": output,
        "output_text": output_text,
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


class _FakeOpenAI:
    configured = True
    model = "test-model"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    async def create_response(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.responses.pop(0)

    async def aclose(self) -> None:
        return None


class _TarantellaMusicBrainz:
    async def search_recordings(
        self, *, artist: str, title: str, limit: int = 10
    ) -> dict[str, Any]:
        assert artist == "Gabry Ponte, KEL"
        assert title == "Tarantella"
        return {
            "recordings": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "title": "Tarantella",
                    "length": 146_000,
                    "artist-credit": [
                        {"name": "Gabry Ponte", "joinphrase": " & "},
                        {"name": "KEL", "joinphrase": ""},
                    ],
                    # Deliberately put the misleading compilation first: provider
                    # ordering must not define either version or canonical release.
                    "releases": [
                        {
                            "id": "22222222-2222-2222-2222-222222222222",
                            "title": "Radio Norba \u2013 Battiti Live Compilation 2024",
                            "status": "Official",
                            "date": "2024-07-01",
                            "release-group": {
                                "id": "33333333-3333-3333-3333-333333333333",
                                "primary-type": "Album",
                                "secondary-types": ["Compilation"],
                            },
                        },
                        {
                            "id": "44444444-4444-4444-8444-444444444444",
                            "title": "Tarantella",
                            "status": "Official",
                            "date": "2024-01-12",
                            "release-group": {
                                "id": "55555555-5555-4555-8555-555555555555",
                                "primary-type": "Single",
                                "secondary-types": [],
                            },
                        },
                    ],
                }
            ][:limit]
        }


class _SourceCancellation:
    def is_set(self) -> bool:
        return False


class _SourceMonitor:
    def raise_if_unusable(self) -> None:
        return None


class _TarantellaYtDlp:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, ProviderIdentity, int]] = []
        self.probe_calls: list[str] = []
        self._entries = {
            "tarantella-live": self._entry(
                "tarantella-live", "Gabry Ponte & KEL - Tarantella (Live)"
            ),
            "tarantella-cover": self._entry(
                "tarantella-cover", "Gabry Ponte & KEL - Tarantella (Cover)"
            ),
            "tarantella-karaoke": self._entry(
                "tarantella-karaoke", "Tarantella (Karaoke)", artist="Karaoke Crew"
            ),
            "tarantella-wrong": self._entry(
                "tarantella-wrong", "Different Artist - Tarantella", artist="Different Artist"
            ),
            "rxw1RCAY3qw": self._entry(
                "rxw1RCAY3qw",
                "Gabry Ponte, KEL - Tarantella (Official Audio)",
                uploader="Gabry Ponte & KEL - Topic",
            ),
        }

    @staticmethod
    def _entry(
        source_id: str,
        title: str,
        *,
        artist: str = "Gabry Ponte & KEL",
        uploader: str = "Third Party Archive",
    ) -> dict[str, object]:
        return {
            "id": source_id,
            "title": title,
            "track": "Tarantella",
            "artist": artist,
            "artists": ["Gabry Ponte", "KEL"] if artist == "Gabry Ponte & KEL" else [artist],
            "uploader": uploader,
            "duration": 146.0,
            "availability": "public",
            "acodec": "opus",
        }

    def search_provider(
        self,
        query: str,
        *,
        provider: ProviderIdentity,
        limit: int,
        cancel_signal: object = None,
    ) -> dict[str, object]:
        del cancel_signal
        self.search_calls.append((query, provider, limit))
        if query.endswith("official audio"):
            values = [self._entries["rxw1RCAY3qw"]]
        elif len(self.search_calls) == 1:
            values = [
                self._entries["tarantella-live"],
                self._entries["tarantella-cover"],
                self._entries["tarantella-karaoke"],
                self._entries["tarantella-wrong"],
            ]
        else:
            values = []
        return {"entries": values[:limit]}

    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        del cancel_signal
        self.probe_calls.append(url)
        return dict(self._entries[url.rsplit("=", 1)[-1]])


def _request(factory, *, text: str, action: str, suffix: str) -> tuple[str, Request]:
    with factory.begin() as session:
        user = User(
            username=f"accuracy-{suffix}",
            username_normalized=f"accuracy-{suffix}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Accuracy")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text=text,
            action=action,
            input_kind="natural_language",
            requested_count=1,
            status="orchestrating",
            idempotency_key=f"accuracy-{suffix}",
        )
        session.add(request)
        session.flush()
        request_id = request.id
    with factory() as session:
        stored = session.get(Request, request_id)
        assert stored is not None
        return request_id, stored


def _proposal(
    *,
    artist: str,
    title: str,
    album: str | None,
    recording_mbid: str,
    release_mbid: str,
    version: str | None = None,
) -> MusicProposal:
    return MusicProposal.model_validate(
        {
            "summary": "One exact recording candidate",
            "clarification": None,
            "exhausted": True,
            "tracks": [
                {
                    "artist": artist,
                    "title": title,
                    "album": album,
                    "album_artist": artist,
                    "year": 2024,
                    "duration_seconds": 146.0,
                    "recording_mbid": recording_mbid,
                    "release_mbid": release_mbid,
                    "release_group_mbid": None,
                    "source_url": None,
                    "version": version,
                    "rationale": (
                        "MusicBrainz identifies this recording as a live version rather than "
                        "a studio release."
                    ),
                    "evidence": [],
                    "confidence": 0.97,
                }
            ],
        }
    )


@pytest.mark.parametrize(("action", "auto_queue"), [("find", False), ("add", True)])
def test_tarantella_exact_find_and_add_use_canonical_release_not_compilation_word_live(
    settings, session_factory, action, auto_queue
):
    request_id, request = _request(
        session_factory,
        text=f"{action} Tarantella by Gabry Ponte & KEL",
        action=action,
        suffix=f"tarantella-{action}",
    )
    recording_mbid = "11111111-1111-1111-1111-111111111111"
    release_mbid = "22222222-2222-2222-2222-222222222222"
    proposal = _proposal(
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        album="Battiti Live Compilation",
        recording_mbid=recording_mbid,
        release_mbid=release_mbid,
    )
    verified = VerifiedMetadata(
        recording_mbid=recording_mbid,
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album="Tarantella",
        duration_seconds=146.0,
        version_signature="studio",
        release_mbid=release_mbid,
        release_group_mbid="33333333-3333-3333-3333-333333333333",
        score=97,
        artists=("Gabry Ponte", "KEL"),
    )
    [track_id] = ProposalService(settings, session_factory).store(
        request_id, proposal, verified_metadata={recording_mbid: verified}
    )
    with session_factory() as session:
        track = session.get(RequestTrack, track_id)
        stored_request = session.get(Request, request_id)
        assert track is not None and stored_request is not None
        assert track.artist == "Gabry Ponte & KEL"
        assert track.album == "Tarantella"
        assert track.version_signature == "studio"
        assert track.canonical_identity_verified is True
        assert track.release_mbid == release_mbid
        assert "live version" not in track.rationale.casefold()
        provenance = json.loads(track.metadata_provenance_json)
        assert provenance["request_constraints"]["requested_version"] is None
        decision = confirmation_decision(stored_request, [track], settings)
    assert decision.auto_queue is auto_queue
    assert request.action == action


@pytest.mark.parametrize("version", ["live", "remix", "acoustic"])
def test_explicit_recording_version_remains_authoritative(settings, session_factory, version):
    qualifier = "recording" if version == "acoustic" else "version"
    request_id, _ = _request(
        session_factory,
        text=f"add Tarantella ({version} {qualifier}) by Gabry Ponte & KEL",
        action="add",
        suffix=f"explicit-{version}",
    )
    recording_mbid = {
        "live": "41111111-1111-1111-1111-111111111111",
        "remix": "51111111-1111-1111-1111-111111111111",
        "acoustic": "61111111-1111-1111-1111-111111111111",
    }[version]
    release_mbid = recording_mbid.replace("1-1111", "2-2222", 1)
    proposal = _proposal(
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album="A release with unrelated wording",
        recording_mbid=recording_mbid,
        release_mbid=release_mbid,
        version="studio",
    )
    verified = VerifiedMetadata(
        recording_mbid=recording_mbid,
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album=f"Tarantella ({version})",
        duration_seconds=146.0,
        version_signature=version,
        release_mbid=release_mbid,
        release_group_mbid=None,
        score=98,
    )
    [track_id] = ProposalService(settings, session_factory).store(
        request_id, proposal, verified_metadata={recording_mbid: verified}
    )
    with session_factory() as session:
        track = session.get(RequestTrack, track_id)
        request = session.get(Request, request_id)
        assert track is not None and request is not None
        assert track.version_signature == version
        assert track.album == f"Tarantella ({version})"
        assert confirmation_decision(request, [track], settings).auto_queue


@pytest.mark.parametrize(
    ("artist", "title"),
    [
        ("Earth, Wind & Fire", "September"),
        ("Live", "Lightning Crashes"),
        ("Simon & Garfunkel", "The Sound of Silence"),
    ],
)
def test_punctuation_bearing_group_names_remain_exact_studio_artists(
    settings, session_factory, artist, title
):
    suffix = str(abs(hash((artist, title))))
    request_id, _ = _request(
        session_factory,
        text=f"add {title} by {artist}",
        action="add",
        suffix=suffix,
    )
    recording_mbid = "71111111-1111-1111-1111-111111111111"
    proposal = _proposal(
        artist=artist,
        title=title,
        album="Studio Album",
        recording_mbid=recording_mbid,
        release_mbid="72222222-2222-2222-2222-222222222222",
    )
    verified = VerifiedMetadata(
        recording_mbid=recording_mbid,
        artist=artist,
        title=title,
        album="Studio Album",
        duration_seconds=146.0,
        version_signature="studio",
        release_mbid="72222222-2222-2222-2222-222222222222",
        release_group_mbid=None,
        score=98,
    )
    [track_id] = ProposalService(settings, session_factory).store(
        request_id, proposal, verified_metadata={recording_mbid: verified}
    )
    with session_factory() as session:
        track = session.get(RequestTrack, track_id)
        request = session.get(Request, request_id)
        assert track is not None and request is not None
        assert track.artist == artist
        assert track.version_signature == "studio"
        assert confirmation_decision(request, [track], settings).auto_queue


def test_direct_url_still_bypasses_model_orchestration(client):
    setup = client.get("/setup")
    response = client.post(
        "/setup",
        data={
            "username": "admin",
            "password": "correct horse battery staple",
            "csrf_token": setup.cookies["music_agent_preauth"],
            "acknowledge_rights": "yes",
        },
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/api/v1/requests",
        json={
            "text": "https://youtu.be/rxw1RCAY3qw?si=offline-fixture",
            "action": "add",
            "conversation_id": None,
        },
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": client.cookies["music_agent_csrf"],
            "Idempotency-Key": "direct-tarantella-fixture",
        },
    )
    assert response.status_code == 200
    request_id = response.json()["request"]["id"]
    assert response.json()["request"]["input_kind"] == "youtube_url"
    with client.app.state.session_factory() as session:
        task = session.scalar(
            select(ServiceTask).where(
                ServiceTask.payload_json
                == json.dumps({"request_id": request_id}, separators=(",", ":"))
            )
        )
    assert task is not None and task.target == "worker" and task.kind == "resolve_direct_request"


@pytest.mark.asyncio
async def test_observed_tarantella_request_reaches_exact_source_without_review(
    settings, session_factory
) -> None:
    """Exercise the complete offline request→canonical→queue→source seam."""

    with session_factory.begin() as session:
        user = User(
            username="tarantella-end-to-end",
            username_normalized="tarantella-end-to-end",
            password_hash="fixture",  # noqa: S106 - inert database fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Exact acquisition")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="gabry ponte, kel - tarantella",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="pending",
            idempotency_key="observed-tarantella-end-to-end",
        )
        session.add(request)
        session.flush()
        request_id = request.id
        user_id = user.id

    registry = ToolRegistry(session_factory)
    register_musicbrainz_tools(registry, _TarantellaMusicBrainz())  # type: ignore[arg-type]
    model_proposal = _proposal(
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        album="Radio Norba \u2013 Battiti Live Compilation 2024",
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
        version="live",
    ).model_dump_json()
    fake_openai = _FakeOpenAI(
        [
            _response(
                [
                    {
                        "type": "function_call",
                        "call_id": "call_tarantella_musicbrainz",
                        "name": "musicbrainz_search_recordings",
                        "arguments": json.dumps(
                            {
                                "artist": "Gabry Ponte, KEL",
                                "title": "Tarantella",
                                "album": None,
                                "duration_seconds": 146.0,
                                "version": None,
                                "limit": 10,
                            },
                            separators=(",", ":"),
                        ),
                    }
                ]
            ),
            _response([], model_proposal),
        ]
    )
    runtime_settings = settings.model_copy(
        update={
            "enabled_media_providers": ["youtube"],
            "media_provider_preference": ["youtube"],
            "ai_match_resolution_enabled": False,
        }
    )
    await OrchestrationService(
        runtime_settings,
        session_factory,
        registry,
        openai_client=fake_openai,  # type: ignore[arg-type]
    ).run_request(request_id)

    with session_factory() as session:
        stored_request = session.get(Request, request_id)
        stored_track = session.scalar(
            select(RequestTrack).where(RequestTrack.request_id == request_id)
        )
        assert stored_request is not None and stored_track is not None
        assert stored_track.version_signature == "studio"
        assert stored_track.album == "Tarantella"
        assert stored_track.release_mbid == "44444444-4444-4444-8444-444444444444"
        assert stored_track.canonical_identity_verified is True
        assert confirmation_decision(stored_request, [stored_track], runtime_settings).auto_queue
        track_id = stored_track.id

    [job_id] = JobRepository(session_factory).queue_approved(request_id, user_id, [track_id])
    queue = DownloadJobQueue(session_factory)
    lease = queue.claim_next()
    assert lease is not None and lease.job_id == job_id
    provider = _TarantellaYtDlp()
    selected = WorkerSourceResolver(
        runtime_settings,
        session_factory,
        queue,
        provider,  # type: ignore[arg-type]
    ).resolve(lease, _SourceMonitor(), _SourceCancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    assert 0 < len(provider.search_calls) <= 6
    assert len(provider.probe_calls) <= 12
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        candidate = session.get(SourceCandidate, job.active_source_candidate_id if job else None)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "acquisition_source",
                JobDecision.state == "selected",
            )
        )
        assert job is not None and job.status == "active"
        assert candidate is not None and candidate.source_id == "rxw1RCAY3qw"
        assert candidate.version_signature == "studio"
        assert decision is not None and decision.decided_by == "deterministic"
        assert (
            session.scalar(select(ServiceTask.id).where(ServiceTask.kind == "select_source"))
            is None
        )
