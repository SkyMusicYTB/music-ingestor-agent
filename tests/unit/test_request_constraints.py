from __future__ import annotations

import json

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Conversation, DownloadJob, Request, RequestTrack, User
from app.repositories.jobs import JobRepository
from app.schemas import MusicProposal
from app.services.proposals import ProposalService
from app.services.request_constraints import parse_explicit_request_constraints
from app.sources import ProviderIdentity
from app.workers.source_resolution import _effective_provider_scope


def test_explicit_constraints_are_conservative_and_user_attributed() -> None:
    parsed = parse_explicit_request_constraints(
        'add Yellow (live version) by Coldplay from album "Live 2012" via YouTube'
    )
    assert parsed.provider == "youtube"
    assert parsed.album == "Live 2012"
    assert parsed.version == "live"

    assert parse_explicit_request_constraints("add Lightning Crashes by Live").version is None
    excluded = parse_explicit_request_constraints("find songs not on YouTube")
    assert excluded.provider is None
    assert excluded.providers == ()
    assert excluded.excluded_providers == ("youtube",)

    alternatives = parse_explicit_request_constraints("add a track from YouTube or via SoundCloud")
    assert alternatives.provider is None
    assert alternatives.providers == ("soundcloud", "youtube")
    assert alternatives.excluded_providers == ()
    assert _effective_provider_scope(alternatives.as_provenance()) == frozenset(
        {ProviderIdentity.SOUNDCLOUD, ProviderIdentity.YOUTUBE}
    )
    assert _effective_provider_scope(excluded.as_provenance()) == frozenset(
        {ProviderIdentity.BANDCAMP, ProviderIdentity.SOUNDCLOUD}
    )


def test_direct_url_is_an_explicit_provider_constraint() -> None:
    parsed = parse_explicit_request_constraints(
        "https://soundcloud.com/example/track",
        input_kind="media_url",
    )
    assert parsed.provider == "soundcloud"
    assert parsed.providers == ("soundcloud",)
    assert parsed.album is None
    assert parsed.version is None


def test_proposal_and_job_preserve_explicit_release_source_and_version_constraints(
    settings: Settings,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        user = User(
            username="constraints",
            username_normalized="constraints",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="constraints")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text='add Yellow (live version) by Coldplay from album "Live 2012" via YouTube',
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="orchestrating",
            idempotency_key="constraints",
        )
        session.add(request)
        session.flush()
        request_id, user_id = request.id, user.id

    proposal = MusicProposal.model_validate(
        {
            "summary": "One recording",
            "clarification": None,
            "exhausted": False,
            "tracks": [
                {
                    "artist": "Coldplay",
                    "title": "Yellow",
                    "album": "Model supplied album",
                    "album_artist": "Coldplay",
                    "year": 2000,
                    "duration_seconds": 266.0,
                    "recording_mbid": None,
                    "release_mbid": None,
                    "release_group_mbid": None,
                    "source_url": None,
                    "version": None,
                    "rationale": "Matches",
                    "evidence": [],
                    "confidence": 0.9,
                }
            ],
        }
    )
    [track_id] = ProposalService(settings, session_factory).store(request_id, proposal)
    [job_id] = JobRepository(session_factory).queue_approved(request_id, user_id, [track_id])

    with session_factory() as session:
        track = session.get(RequestTrack, track_id)
        job = session.get(DownloadJob, job_id)
        assert track is not None and job is not None
        provenance = json.loads(track.metadata_provenance_json)
        constraints = provenance["request_constraints"]
        snapshot = json.loads(job.approved_snapshot_json)
    assert track.version_signature == "live"
    assert provenance["album_constraint_explicit"] is True
    assert constraints["requested_provider"] == "youtube"
    assert constraints["requested_album"] == "Live 2012"
    assert constraints["requested_version"] == "live"
    assert snapshot["requested_provider"] == "youtube"
    assert snapshot["requested_providers"] == ["youtube"]
    assert snapshot["excluded_providers"] == []
    assert snapshot["provider_fallback_allowed"] is False
    assert snapshot["requested_album"] == "Live 2012"
    assert snapshot["requested_version"] == "live"
