from __future__ import annotations

import json
from dataclasses import replace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Conversation, DownloadJob, Request, RequestTrack, User
from app.repositories.jobs import JobRepository
from app.schemas import MusicProposal, ProposalTrack
from app.services.orchestration import (
    _canonical_evidence_compatible,
    _proposal_recording_version,
)
from app.services.proposals import ProposalService, VerifiedMetadata
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
    assert parse_explicit_request_constraints("add Song (Live Forever) by Oasis").version is None
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


def test_release_names_do_not_become_recording_version_constraints() -> None:
    for request, expected_album in (
        ('add Studio Song from album "Live at Wembley" by Artist', "Live at Wembley"),
        ('add Studio Song from album "Acoustic Version" by Artist', "Acoustic Version"),
        ('add Studio Song from album "Radio Edit" by Artist', "Radio Edit"),
        ('add Studio Song from release "Live from New York" by Artist', "Live from New York"),
        ("add Studio Song album: Live at Wembley by Artist", "Live at Wembley"),
    ):
        parsed = parse_explicit_request_constraints(request)
        assert parsed.album == expected_album
        assert parsed.version is None


def test_version_outside_release_name_remains_explicit() -> None:
    parsed = parse_explicit_request_constraints(
        'add the live version of Yellow from album "Live 2012" by Coldplay'
    )
    assert parsed.album == "Live 2012"
    assert parsed.version == "live"


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


@pytest.mark.parametrize("verified_version", ["studio", "live"])
def test_non_explicit_version_comes_only_from_verified_recording_evidence(
    settings: Settings,
    session_factory: sessionmaker[Session],
    verified_version: str,
) -> None:
    recording_mbid = "24f4e1df-a51a-4dc4-a0a3-28f8dd66a011"
    with session_factory.begin() as session:
        user = User(
            username=f"verified-{verified_version}",
            username_normalized=f"verified-{verified_version}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title=f"verified {verified_version}")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Tarantella by Gabry Ponte and KEL",
            action="add",
            input_kind="natural_language",
            requested_count=1,
            status="orchestrating",
            idempotency_key=f"verified-{verified_version}",
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
                    "artist": "Gabry Ponte, KEL",
                    "title": "Tarantella",
                    "album": "Radio Italia Live Compilation",
                    "album_artist": "Various Artists",
                    "year": 2024,
                    "duration_seconds": 146,
                    "recording_mbid": recording_mbid,
                    "release_mbid": None,
                    "release_group_mbid": None,
                    "source_url": None,
                    # Model text is descriptive, not an explicit user constraint.
                    "version": "live",
                    "rationale": "Matches",
                    "evidence": [],
                    "confidence": 0.9,
                }
            ],
        }
    )
    verified = VerifiedMetadata(
        recording_mbid=recording_mbid,
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album="Tarantella",
        duration_seconds=146,
        version_signature=verified_version,
        release_mbid=None,
        release_group_mbid=None,
        score=95,
        year=2024,
        artists=("Gabry Ponte", "KEL"),
    )
    [track_id] = ProposalService(settings, session_factory).store(
        request_id, proposal, verified_metadata={recording_mbid: verified}
    )
    [job_id] = JobRepository(session_factory).queue_approved(request_id, user_id, [track_id])
    with session_factory() as session:
        track = session.get(RequestTrack, track_id)
        job = session.get(DownloadJob, job_id)
        assert track is not None and job is not None
        provenance = json.loads(track.metadata_provenance_json)
        snapshot = json.loads(job.approved_snapshot_json)
        assert track.version_signature == verified_version
        assert track.canonical_identity_verified is True
        assert (track.artist, track.album, track.album_artist, track.year) == (
            "Gabry Ponte & KEL",
            "Tarantella",
            "Gabry Ponte & KEL",
            2024,
        )
        assert provenance["artists"] == ["Gabry Ponte", "KEL"]
        assert provenance["recording_version"] == {
            "signature": verified_version,
            "source": "musicbrainz_recording_disambiguation",
        }
        assert snapshot["artists"] == ["Gabry Ponte", "KEL"]


def test_orchestration_recording_identity_ignores_release_text_and_requires_full_credit() -> None:
    proposed = ProposalTrack.model_validate(
        {
            "artist": "Gabry Ponte, KEL",
            "title": "Tarantella",
            "album": "Radio Italia Live Compilation",
            "album_artist": "Various Artists",
            "year": 2024,
            "duration_seconds": 146,
            "recording_mbid": None,
            "release_mbid": None,
            "release_group_mbid": None,
            "source_url": None,
            # A model may echo "live" from the compilation name. It is not a
            # user constraint and must not redefine this recording.
            "version": "live",
            "rationale": "fixture",
            "evidence": [],
            "confidence": 0.9,
        }
    )
    verified = VerifiedMetadata(
        recording_mbid="24f4e1df-a51a-4dc4-a0a3-28f8dd66a011",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        album="Tarantella",
        duration_seconds=146,
        version_signature="studio",
        release_mbid=None,
        release_group_mbid=None,
        score=95,
        artists=("Gabry Ponte", "KEL"),
    )
    assert _proposal_recording_version(proposed) == "studio"
    assert _canonical_evidence_compatible(proposed, verified)
    proposed_with_verified_id = proposed.model_copy(
        update={"recording_mbid": verified.recording_mbid}
    )
    live_verified = replace(verified, version_signature="live")
    assert (
        _proposal_recording_version(
            proposed_with_verified_id,
            verified_metadata={verified.recording_mbid: live_verified},
        )
        == "live"
    )
    missing_collaborator = replace(verified, artist="Gabry Ponte")
    assert not _canonical_evidence_compatible(proposed, missing_collaborator)
