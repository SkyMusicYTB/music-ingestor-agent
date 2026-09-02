from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import Track, User
from app.repositories.auth import AuthRepository, SetupAlreadyCompleted
from app.repositories.requests import classify_input, parse_requested_count
from app.services.confirmation import confirmation_decision
from app.services.duplicates import (
    DuplicateCandidate,
    DuplicateDetector,
    normalize_text,
    version_signature,
    versions_compatible,
)


def test_normalization_removes_only_provider_suffixes() -> None:
    assert normalize_text("Numb (Official Video) [HD]") == "numb"
    assert normalize_text("Song (Live)") == "song live"
    assert version_signature("Song (Live at Wembley)") == "live"
    assert "remaster" in version_signature("Song - 2024 Remastered")
    assert version_signature("Song (Acoustic)") == "acoustic"
    assert not versions_compatible("studio", "live")


def test_requested_count_is_extracted_without_model_authority() -> None:
    assert parse_requested_count("find 15-track guitar songs", input_kind="natural_language") == 15
    assert parse_requested_count("recommend twelve songs", input_kind="natural_language") == 12
    assert parse_requested_count("Numb by Linkin Park", input_kind="natural_language") is None
    assert parse_requested_count("https://youtu.be/abcdefghijk", input_kind="youtube_url") == 1
    collection_url = "https://artist.bandcamp.com/album/a-reviewed-album"
    assert classify_input(collection_url) == "media_collection_url"
    assert parse_requested_count(collection_url, input_kind="media_collection_url") is None


def test_first_admin_insert_is_atomic(engine, session_factory, settings) -> None:
    repository = AuthRepository(engine, session_factory, settings)

    def attempt(name: str) -> str:
        try:
            repository.create_initial_admin(name, "a production length password")
        except SetupAlreadyCompleted:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["admin-one", "admin-two"]))
    assert sorted(results) == ["lost", "won"]
    with session_factory() as session:
        assert len(list(session.scalars(select(User)))) == 1


def test_session_csrf_is_hashed(engine, session_factory, settings) -> None:
    repository = AuthRepository(engine, session_factory, settings)
    user_id = repository.create_initial_admin("admin", "a production length password")
    created = repository.create_session(user_id)
    resolved = repository.resolve_session(created.token)
    assert resolved is not None
    assert repository.csrf_matches(resolved, created.csrf_token)
    assert not repository.csrf_matches(resolved, "different-token")


def test_duplicate_detector_revalidates_missing_file(
    session_factory, settings, tmp_path: Path
) -> None:
    existing_path = settings.music_path / "Artist" / "Album" / "01 - Song.mp3"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(b"present")
    with session_factory.begin() as session:
        track = Track(
            artist="Artist",
            artist_normalized="artist",
            title="Song",
            title_normalized="song",
            album="Album",
            version_signature="studio",
            filepath="Artist/Album/01 - Song.mp3",
            is_present=True,
            file_mtime_ns=1,
            file_size=7,
            scan_generation=1,
        )
        session.add(track)
        session.flush()
        track_id = track.id
    detector = DuplicateDetector(settings.music_path)
    with session_factory.begin() as session:
        decision = detector.find(session, DuplicateCandidate(artist="Artist", title="Song"))
    assert decision.status == "owned"
    existing_path.unlink()
    with session_factory.begin() as session:
        decision = detector.find(session, DuplicateCandidate(artist="Artist", title="Song"))
    assert decision.status == "none"
    with session_factory() as session:
        assert session.get(Track, track_id).is_present is False


def test_confirmation_policy_never_auto_queues_find(settings) -> None:
    class Candidate:
        selected = True
        duplicate_status = "none"
        metadata_confidence = 1.0
        recording_mbid = "recording"
        source_extractor = None
        source_id = None

    class Request:
        action = "find"
        input_kind = "natural_language"
        requested_count = 1

    result = confirmation_decision(Request(), [Candidate()], settings)
    assert result.auto_queue is False


def test_confirmation_accepts_only_an_explicit_exact_direct_add(settings) -> None:
    class Candidate:
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.8
        recording_mbid = None
        source_extractor = "youtube"
        source_id = "abcdefghijk"

    class Request:
        action = "add"
        input_kind = "youtube_url"
        requested_count = 1

    result = confirmation_decision(Request(), [Candidate()], settings)
    assert result.auto_queue is True


def test_fuzzy_single_result_cannot_become_an_exact_add_from_model_output(settings) -> None:
    class Candidate:
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.99
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        source_extractor = None
        source_id = None
        metadata_provenance_json = json.dumps(
            {
                "automatic_association": True,
                "source": "musicbrainz_search_recordings",
                "recording_mbid": recording_mbid,
                "score": 99,
            }
        )

    class Request:
        action = "add"
        input_kind = "natural_language"
        requested_count = 1

        def __init__(self, raw_text: str) -> None:
            self.raw_text = raw_text

    for raw_text in (
        "add a dreamy track",
        "add something similar to Yellow by Coldplay",
        "add some popular music",
        "add a song by Coldplay",
        "add a sad song",
        "add a 90s hit",
        "add the newest Coldplay single",
    ):
        result = confirmation_decision(Request(raw_text), [Candidate()], settings)
        assert result.auto_queue is False
        assert "original request" in result.reason


def test_explicit_title_and_artist_remain_exact_even_for_a_generic_real_title(settings) -> None:
    class Candidate:
        artist = "The Beatles"
        title = "Something"
        album = "Abbey Road"
        version_signature = "studio"
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.96
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        source_extractor = None
        source_id = None
        metadata_provenance_json = json.dumps(
            {
                "automatic_association": True,
                "source": "musicbrainz_search_recordings",
                "recording_mbid": "11111111-1111-1111-1111-111111111111",
                "score": 96,
            }
        )

    class Request:
        action = "add"
        input_kind = "natural_language"
        requested_count = 1
        raw_text = "add Something by The Beatles"

    result = confirmation_decision(Request(), [Candidate()], settings)
    assert result.auto_queue is True


@pytest.mark.parametrize(
    "source_constraint",
    [
        "from SoundCloud or via Bandcamp",
        "from SoundCloud, not from YouTube",
        "without YouTube",
        "Bandcamp only",
    ],
)
def test_exact_autoqueue_ignores_only_explicit_source_qualifiers(
    settings, source_constraint: str
) -> None:
    class Candidate:
        artist = "Coldplay"
        title = "Yellow"
        album = "Parachutes"
        version_signature = "studio"
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.96
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        source_extractor = None
        source_id = None
        metadata_provenance_json = json.dumps(
            {
                "automatic_association": True,
                "source": "musicbrainz_search_recordings",
                "recording_mbid": recording_mbid,
                "score": 96,
            }
        )

    class Request:
        action = "add"
        input_kind = "natural_language"
        requested_count = 1
        raw_text = f"add Yellow by Coldplay {source_constraint}"

    assert confirmation_decision(Request(), [Candidate()], settings).auto_queue is True


def test_exact_autoqueue_is_bound_to_the_authoritative_requested_identity(settings) -> None:
    class Candidate:
        artist = "Radiohead"
        title = "Creep"
        album = "Pablo Honey"
        version_signature = "studio"
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.99
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        source_extractor = None
        source_id = None
        metadata_provenance_json = json.dumps(
            {
                "automatic_association": True,
                "source": "musicbrainz_search_recordings",
                "recording_mbid": recording_mbid,
                "score": 99,
            }
        )

    class Request:
        action = "add"
        input_kind = "natural_language"
        requested_count = 1
        raw_text = "add Yellow by Coldplay"

    result = confirmation_decision(Request(), [Candidate()], settings)
    assert result.auto_queue is False
    assert "authoritative exact-track identity" in result.reason


@pytest.mark.parametrize(
    "raw_text",
    [
        "add Yellow and Clocks by Coldplay",
        "add Yellow, Clocks by Coldplay",
    ],
)
def test_partial_bulk_result_cannot_autoqueue_as_an_exact_add(settings, raw_text: str) -> None:
    class Candidate:
        artist = "Coldplay"
        title = "Yellow"
        album = "Parachutes"
        version_signature = "studio"
        selected = True
        duplicate_status = "none"
        metadata_confidence = 0.99
        recording_mbid = "11111111-1111-1111-1111-111111111111"
        source_extractor = None
        source_id = None
        metadata_provenance_json = json.dumps(
            {
                "automatic_association": True,
                "source": "musicbrainz_search_recordings",
                "recording_mbid": recording_mbid,
                "score": 99,
            }
        )

    class Request:
        action = "add"
        input_kind = "natural_language"
        requested_count = None

        def __init__(self, text: str) -> None:
            self.raw_text = text

    assert confirmation_decision(Request(raw_text), [Candidate()], settings).auto_queue is False


def test_collection_origin_never_autoqueues_a_single_surviving_entry(settings) -> None:
    class Candidate:
        selected = True
        duplicate_status = "none"
        metadata_confidence = 1.0
        recording_mbid = None
        source_extractor = "bandcamp"
        source_id = "yellow"
        metadata_provenance_json = json.dumps({"source": "validated_direct_collection_metadata"})

    class Request:
        action = "add"
        input_kind = "media_collection_url"
        requested_count = None
        raw_text = "https://coldplay.bandcamp.com/album/parachutes"

    result = confirmation_decision(Request(), [Candidate()], settings)
    assert result.auto_queue is False
    assert "collection entries" in result.reason


def test_production_secrets_come_only_from_credential_files(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "auth_hmac_key").write_text("credential-hmac", encoding="utf-8")
    (credentials / "openai_api_key").write_text("credential-openai", encoding="utf-8")
    settings = Settings(
        environment="production",
        credential_directory=credentials,
        auth_hmac_key="untrusted-environment-hmac",
        openai_api_key="untrusted-environment-openai",
        **MANAGED_PRODUCTION_PATHS,
    )
    assert settings.auth_hmac_key.get_secret_value() == "credential-hmac"
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "credential-openai"


def test_production_worker_is_secret_free_and_needs_no_credentials(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        service_role="worker",
        openai_api_key="must-be-discarded",
        listenbrainz_token="must-also-be-discarded",  # noqa: S106 - proves worker scrubbing
        **MANAGED_PRODUCTION_PATHS,
    )
    assert settings.openai_api_key is None
    assert settings.listenbrainz_token is None


MANAGED_PRODUCTION_PATHS = {
    "database_path": Path("/var/lib/music-agent/music-agent.db"),
    "artwork_path": Path("/var/lib/music-agent/artwork"),
    "downloads_path": Path("/srv/music-downloads"),
    "music_path": Path("/srv/music"),
    "backup_path": Path("/var/lib/music-agent/backups"),
}
