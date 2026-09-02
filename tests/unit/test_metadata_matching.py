from __future__ import annotations

from app.services.metadata_matching import (
    MetadataCandidate,
    MetadataMatcher,
    ReleaseMetadataCandidate,
    ReleaseMetadataMatcher,
    candidates_from_apple,
    candidates_from_musicbrainz,
    normalize_text,
)
from app.workers.metadata import _selected_release_summary, _with_sensible_release


def test_metadata_matcher_prefers_correct_version_and_duration() -> None:
    candidates = [
        MetadataCandidate(
            artist="Beyoncé",
            title="Halo (Live)",
            duration_seconds=260,
            source="musicbrainz",
        ),
        MetadataCandidate(
            artist="Beyonce",
            title="Halo",
            duration_seconds=242,
            source="musicbrainz",
        ),
    ]
    results = MetadataMatcher().rank(
        artist="Beyoncé",
        title="Halo",
        duration_seconds=241,
        candidates=candidates,
    )

    assert results[0].candidate.title == "Halo"
    assert results[0].score > results[1].score
    assert normalize_text("Beyoncé & Jay-Z") == "beyonce and jay z"


def test_recording_match_uses_locked_weights_and_eight_point_lead() -> None:
    exact = MetadataCandidate(
        artist="Artist",
        title="Song",
        album="Album",
        duration_seconds=200,
        recording_mbid="00000000-0000-4000-8000-000000000001",
    )
    wrong_duration = MetadataCandidate(
        artist="Artist",
        title="Song",
        album="Album",
        duration_seconds=260,
        recording_mbid="00000000-0000-4000-8000-000000000002",
    )
    ranked = MetadataMatcher().rank(
        artist="Artist",
        title="Song",
        album="Album",
        duration_seconds=200,
        candidates=[exact, wrong_duration],
    )

    assert ranked[0].score == 100
    assert ranked[0].decision == "auto"
    assert ranked[0].lead == 15
    assert ranked[1].score == 85
    assert any("duration=15.0/15" in reason for reason in ranked[0].reasons)

    tied = MetadataMatcher().rank(
        artist="Artist",
        title="Song",
        album="Album",
        duration_seconds=200,
        candidates=[exact, exact],
    )
    assert tied[0].score == 100
    assert tied[0].lead == 0
    assert tied[0].decision == "review"


def test_explicit_version_mismatch_cannot_auto_associate() -> None:
    ranked = MetadataMatcher().rank(
        artist="Coldplay",
        title="Yellow",
        duration_seconds=266,
        requested_version="live",
        version_is_explicit=True,
        candidates=[
            MetadataCandidate(
                artist="Coldplay",
                title="Yellow",
                album="Parachutes",
                duration_seconds=266,
            )
        ],
    )

    assert ranked[0].score >= 88
    assert ranked[0].decision == "review"
    assert ranked[0].contradiction_codes == ("explicit_version_mismatch",)


def test_explicit_album_mismatch_or_missing_album_cannot_auto_associate() -> None:
    for candidate_album, expected_code in (
        ("Xylophone Dreams", "explicit_album_mismatch"),
        (None, "explicit_album_missing"),
    ):
        ranked = MetadataMatcher().rank(
            artist="Coldplay",
            title="Yellow",
            album="Parachutes",
            album_is_explicit=True,
            duration_seconds=266,
            candidates=[
                MetadataCandidate(
                    artist="Coldplay",
                    title="Yellow",
                    album=candidate_album,
                    duration_seconds=266,
                )
            ],
        )

        assert ranked[0].score >= 88
        assert ranked[0].decision == "review"
        assert ranked[0].contradiction_codes == (expected_code,)


def test_compatible_explicit_album_outranks_a_higher_scoring_contradiction() -> None:
    compatible = MetadataCandidate(
        artist="Coldplay",
        title="Yellow",
        album="Parachutes",
        duration_seconds=250,
        recording_mbid="00000000-0000-4000-8000-000000000001",
    )
    contradictory = MetadataCandidate(
        artist="Coldplay",
        title="Yellow",
        album="Unrelated Release",
        duration_seconds=266,
        recording_mbid="00000000-0000-4000-8000-000000000002",
    )

    ranked = MetadataMatcher().rank(
        artist="Coldplay",
        title="Yellow",
        album="Parachutes",
        album_is_explicit=True,
        duration_seconds=266,
        candidates=[contradictory, compatible],
    )

    assert ranked[0].candidate is compatible
    assert ranked[0].contradiction_codes == ()


def test_release_match_uses_locked_weights_and_edition_penalties() -> None:
    exact = ReleaseMetadataCandidate(
        album="Album",
        status="Official",
        primary_type="Album",
        recording_fit=1,
        version_fit=1,
        duration_fit=1,
        track_placement_fit=1,
        original_year=2000,
        release_mbid="00000000-0000-4000-8000-000000000001",
    )
    deluxe = ReleaseMetadataCandidate(
        album="Album (Deluxe Edition)",
        status="Official",
        primary_type="Album",
        recording_fit=1,
        version_fit=1,
        duration_fit=1,
        track_placement_fit=1,
        original_year=2000,
        edition="Deluxe",
        release_mbid="00000000-0000-4000-8000-000000000002",
    )
    ranked = ReleaseMetadataMatcher().rank(
        requested_album="Album",
        requested_primary_type="Album",
        requested_version=None,
        requested_year=2000,
        candidates=[exact, deluxe],
    )

    assert ranked[0].score == 100
    assert ranked[0].decision == "auto"
    assert ranked[0].lead is not None and ranked[0].lead >= 8
    assert ranked[1].score < 88
    assert any("unrequested-deluxe" in reason for reason in ranked[1].reasons)


def test_provider_payloads_are_parsed_without_inventing_ids() -> None:
    mbid = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
    musicbrainz = candidates_from_musicbrainz(
        {
            "recordings": [
                {
                    "id": mbid,
                    "title": "Track",
                    "length": 123000,
                    "first-release-date": "2001-01-01",
                    "artist-credit": [{"name": "Artist"}],
                    "releases": [],
                }
            ]
        }
    )
    apple = candidates_from_apple(
        {
            "results": [
                {
                    "kind": "song",
                    "artistName": "Artist",
                    "trackName": "Track",
                    "collectionName": "Album",
                    "releaseDate": "2002-03-04T00:00:00Z",
                    "trackTimeMillis": 124000,
                }
            ]
        }
    )

    assert musicbrainz[0].recording_mbid == mbid
    assert musicbrainz[0].duration_seconds == 123
    assert apple[0].recording_mbid is None
    assert apple[0].year == 2002


def test_worker_selects_original_official_standard_release_without_review() -> None:
    candidate = MetadataCandidate(
        artist="Coldplay",
        title="Yellow",
        recording_mbid="cc197bad-dc9c-440d-a5b5-d52ba2e14234",
        raw={
            "first-release-date": "2000-06-26",
            "releases": [
                {
                    "id": "00000000-0000-4000-8000-000000000001",
                    "title": "Now That's What I Call Music",
                    "status": "Official",
                    "date": "1999-01-01",
                    "release-group": {
                        "id": "10000000-0000-4000-8000-000000000001",
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
                },
                {
                    "id": "00000000-0000-4000-8000-000000000002",
                    "title": "Parachutes",
                    "status": "Official",
                    "date": "2000-07-10",
                    "release-group": {
                        "id": "10000000-0000-4000-8000-000000000002",
                        "primary-type": "Album",
                        "secondary-types": [],
                    },
                },
                {
                    "id": "00000000-0000-4000-8000-000000000003",
                    "title": "Parachutes (Deluxe Edition)",
                    "status": "Official",
                    "date": "2001-07-10",
                    "release-group": {
                        "id": "10000000-0000-4000-8000-000000000003",
                        "primary-type": "Album",
                        "secondary-types": [],
                    },
                },
            ],
        },
    )

    selected = _with_sensible_release(candidate, requested_album=None)
    assert selected.album == "Parachutes"
    assert selected.year == 2000
    assert selected.release_mbid == "00000000-0000-4000-8000-000000000002"
    assert _selected_release_summary(selected) == {
        "release_status": "Official",
        "primary_type": "Album",
    }

    explicitly_requested = _with_sensible_release(
        candidate, requested_album="Parachutes (Deluxe Edition)"
    )
    assert explicitly_requested.release_mbid == "00000000-0000-4000-8000-000000000003"
