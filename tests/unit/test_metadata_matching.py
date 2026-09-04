from __future__ import annotations

from app.services.metadata_matching import (
    MetadataCandidate,
    MetadataMatcher,
    ReleaseMetadataCandidate,
    ReleaseMetadataMatcher,
    candidates_from_apple,
    candidates_from_musicbrainz,
    normalize_text,
    release_edition_signature,
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

    assert ranked[0].score < 88
    assert ranked[0].decision == "review"
    assert ranked[0].contradiction_codes == ("explicit_version_mismatch",)
    assert any("explicit-version-mismatch" in reason for reason in ranked[0].reasons)


def test_explicit_version_candidate_keeps_a_decisive_lead_over_studio() -> None:
    for version in ("live", "remix", "acoustic"):
        requested = MetadataCandidate(
            artist="Artist",
            title=f"Song ({version.title()})",
            album="Original Release",
            duration_seconds=200,
            recording_mbid=f"00000000-0000-4000-8000-0000000000{len(version):02d}",
        )
        studio = MetadataCandidate(
            artist="Artist",
            title="Song",
            album="Original Release",
            duration_seconds=200,
            recording_mbid=f"10000000-0000-4000-8000-0000000000{len(version):02d}",
        )

        ranked = MetadataMatcher().rank(
            artist="Artist",
            title="Song",
            requested_version=version,
            version_is_explicit=True,
            duration_seconds=200,
            candidates=[studio, requested],
        )

        assert ranked[0].candidate is requested
        assert ranked[0].score - ranked[1].score >= 8
        assert ranked[0].decision == "auto"


def test_model_album_text_does_not_authorize_a_compilation_edition() -> None:
    candidate = MetadataCandidate(
        artist="Artist",
        title="Song",
        album="Battiti Live Compilation",
        duration_seconds=200,
    )

    untrusted = MetadataMatcher().rank(
        artist="Artist",
        title="Song",
        album="Battiti Live Compilation",
        album_is_explicit=False,
        duration_seconds=200,
        candidates=[candidate],
    )[0]
    explicit = MetadataMatcher().rank(
        artist="Artist",
        title="Song",
        album="Battiti Live Compilation",
        album_is_explicit=True,
        duration_seconds=200,
        candidates=[candidate],
    )[0]

    assert any("unrequested-compilation" in reason for reason in untrusted.reasons)
    assert not any("unrequested-compilation" in reason for reason in explicit.reasons)


def test_missing_required_collaborator_cannot_auto_associate() -> None:
    main_artist = "A Very Long Canonical Main Artist Name"
    ranked = MetadataMatcher().rank(
        artist=f"{main_artist} & X",
        artists=(main_artist, "X"),
        title="Exact Song",
        duration_seconds=200,
        candidates=[
            MetadataCandidate(
                artist=main_artist,
                artists=(main_artist,),
                title="Exact Song",
                duration_seconds=200,
            )
        ],
    )

    assert ranked[0].score >= 88
    assert ranked[0].decision != "auto"
    assert "artist_credit_mismatch" in ranked[0].contradiction_codes


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


def test_release_title_cannot_change_recording_version_and_standard_release_wins() -> None:
    candidate = candidates_from_musicbrainz(
        {
            "recordings": [
                {
                    "id": "24f4e1df-a51a-4dc4-a0a3-28f8dd66a011",
                    "title": "Tarantella",
                    "length": 146000,
                    "artist-credit": [
                        {"name": "Gabry Ponte", "joinphrase": " & "},
                        {"name": "KEL", "joinphrase": ""},
                    ],
                    "releases": [
                        {
                            "id": "53674b6e-0df0-4631-87d0-83146155e169",
                            "title": "Radio Italia Live Compilation",
                            "status": "Official",
                            "date": "2023-01-01",
                            "release-group": {
                                "id": "ad1275fe-dce8-4cf4-bfc8-84aeeb4ae66f",
                                "primary-type": "Album",
                                "secondary-types": ["Compilation"],
                            },
                        },
                        {
                            "id": "bb7c6979-b9ad-43cf-a264-387ec53a817f",
                            "title": "Tarantella",
                            "status": "Official",
                            "date": "2024-04-12",
                            "release-group": {
                                "id": "28738316-cba2-43c6-938b-d156669e0e82",
                                "primary-type": "Single",
                                "secondary-types": [],
                            },
                        },
                    ],
                }
            ]
        }
    )[0]

    # The recording is studio even while one release title contains "Live".
    assert candidate.version == "studio"
    assert candidate.artists == ("Gabry Ponte", "KEL")
    assert candidate.album == "Tarantella"
    assert candidate.release_mbid == "bb7c6979-b9ad-43cf-a264-387ec53a817f"
    assert release_edition_signature("Radio Italia Live Compilation") == (
        "compilation",
        "live_event",
    )
    ranked = MetadataMatcher().rank(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146,
        candidates=[candidate],
    )
    assert ranked[0].decision == "auto"
    selected = _with_sensible_release(candidate, requested_album=None)
    assert selected.album == "Tarantella"
    assert selected.release_mbid == "bb7c6979-b9ad-43cf-a264-387ec53a817f"
    assert selected.version == "studio"


def test_recording_title_and_disambiguation_preserve_explicit_special_version() -> None:
    album_only = MetadataCandidate(artist="Artist", title="Studio Song", album="Live")
    title_version = MetadataCandidate(artist="Artist", title="Song (Live)")
    disambiguated = MetadataCandidate(
        artist="Artist",
        title="Song",
        raw={"disambiguation": "live recording at Wembley", "releases": []},
    )
    assert album_only.version == "studio"
    assert title_version.version == "live"
    assert disambiguated.version == "live"

    ranked = MetadataMatcher().rank(
        artist="Artist",
        title="Song (Live)",
        candidates=[title_version, MetadataCandidate(artist="Artist", title="Song")],
    )
    assert ranked[0].candidate is title_version


def test_sensible_release_prefers_earlier_official_single_over_later_album_same_year() -> None:
    recording_id = "24f4e1df-a51a-4dc4-a0a3-28f8dd66a011"
    selected = candidates_from_musicbrainz(
        {
            "recordings": [
                {
                    "id": recording_id,
                    "title": "Song",
                    "artist-credit": [{"name": "Artist"}],
                    "releases": [
                        {
                            "id": "53674b6e-0df0-4631-87d0-83146155e169",
                            "title": "Later Album",
                            "status": "Official",
                            "date": "2024-12-01",
                            "release-group": {
                                "primary-type": "Album",
                                "secondary-types": [],
                            },
                        },
                        {
                            "id": "bb7c6979-b9ad-43cf-a264-387ec53a817f",
                            "title": "Original Single",
                            "status": "Official",
                            "date": "2024-01-01",
                            "release-group": {
                                "primary-type": "Single",
                                "secondary-types": [],
                            },
                        },
                    ],
                }
            ]
        }
    )[0]

    assert selected.album == "Original Single"
    assert selected.release_mbid == "bb7c6979-b9ad-43cf-a264-387ec53a817f"
    assert set(release_edition_signature("Anniversary soundtrack reissue")) == {
        "deluxe",
        "reissue",
        "soundtrack",
    }


def test_sensible_release_never_cross_wires_release_and_release_group_ids() -> None:
    old_release = "53674b6e-0df0-4631-87d0-83146155e169"
    old_group = "ad1275fe-dce8-4cf4-bfc8-84aeeb4ae66f"
    selected_release = "bb7c6979-b9ad-43cf-a264-387ec53a817f"
    candidate = MetadataCandidate(
        artist="Artist",
        title="Song",
        album="Old Compilation",
        recording_mbid="24f4e1df-a51a-4dc4-a0a3-28f8dd66a011",
        release_mbid=old_release,
        release_group_mbid=old_group,
        raw={
            "releases": [
                {
                    "id": old_release,
                    "title": "Old Compilation",
                    "status": "Official",
                    "date": "2000-01-01",
                    "release-group": {
                        "id": old_group,
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
                },
                {
                    "id": selected_release,
                    "title": "Original Single",
                    "status": "Official",
                    "date": "2001-01-01",
                    "release-group": {
                        "primary-type": "Single",
                        "secondary-types": [],
                    },
                },
            ]
        },
    )

    selected = _with_sensible_release(candidate, requested_album=None)

    assert selected.release_mbid == selected_release
    assert selected.release_group_mbid is None
