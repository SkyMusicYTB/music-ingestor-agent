from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.library_metadata import _read_tags
from app.tags import MediaTags
from app.tags.models import coerce_media_tags
from app.tags.provenance import MAX_PROVENANCE_BYTES, encoded_provenance, sanitized_provenance
from app.workers.processor import DownloadJobProcessor


def test_provider_metadata_policy_defaults_and_legacy_environment(monkeypatch):
    monkeypatch.delenv("MUSIC_AGENT_CANONICAL_METADATA_POLICY", raising=False)
    monkeypatch.delenv("MUSIC_AGENT_PROVIDER_METADATA_FALLBACK_MIN_SCORE", raising=False)
    monkeypatch.setenv("MUSIC_AGENT_MAX_AGENT_STEPS", "50")
    settings = Settings()
    assert settings.canonical_metadata_policy == "prefer"
    assert settings.provider_metadata_fallback_min_score == 0.90
    assert settings.max_model_rounds == 50
    monkeypatch.setenv("MUSIC_AGENT_CANONICAL_METADATA_POLICY", "require")
    monkeypatch.setenv("MUSIC_AGENT_PROVIDER_METADATA_FALLBACK_MIN_SCORE", "0.95")
    assert Settings().canonical_metadata_policy == "require"
    assert Settings().provider_metadata_fallback_min_score == 0.95


@pytest.mark.parametrize("value", ["disabled", "optional", "", "PREFER"])
def test_provider_metadata_policy_rejects_unknown_values(value):
    with pytest.raises(ValidationError):
        Settings(canonical_metadata_policy=value)


@pytest.mark.parametrize("value", [0.0, 0.879, 1.01, float("nan"), float("inf")])
def test_provider_metadata_fallback_floor_is_conservative(value):
    with pytest.raises(ValidationError):
        Settings(provider_metadata_fallback_min_score=value)


@pytest.mark.parametrize(
    "authority",
    ["validated_provider", "direct_user_source", "user_confirmed_provider_metadata"],
)
def test_provider_tag_authority_never_promotes_suggested_mbids(authority):
    tags = MediaTags(
        title="Tarantella",
        artists=("Gabry Ponte", "KEL"),
        recording_mbid="11111111-1111-1111-1111-111111111111",
        release_mbid="22222222-2222-2222-2222-222222222222",
        release_group_mbid="33333333-3333-3333-3333-333333333333",
        metadata_authority=authority,
        canonical_identity_verified=True,
    )
    assert tags.canonical_identity_verified is False
    assert tags.recording_mbid is tags.release_mbid is tags.release_group_mbid is None


def test_provenance_is_bounded_and_excludes_provider_payloads_and_secrets():
    unsafe = {
        "raw_response": "do not persist",
        "password": "do not persist",
        "source_url": "do not persist in opaque payload",
        "reason_code": "zero_candidates",
        "local_score": 0.95,
        "model_confidence": float("nan"),
        "prompt_version": "💿" * 1000,
        "decision_fingerprint": "f" * 5000,
        "decided_by": "deterministic\x00\n",
    }
    encoded = encoded_provenance(unsafe)
    assert encoded is not None and len(encoded.encode("utf-8")) <= MAX_PROVENANCE_BYTES
    result = json.loads(encoded)
    assert result["reason_code"] == "zero_candidates"
    assert result["local_score"] == 0.95
    assert result["decided_by"] == "deterministic"
    assert "model_confidence" not in result
    assert "raw_response" not in result
    assert "password" not in result
    assert "source_url" not in result
    assert sanitized_provenance("x" * 2000) == {}
    assert sanitized_provenance("{malformed}") == {}


def test_scanner_does_not_restore_stale_mbids_from_unverified_source_tags():
    values = {
        "artist": ["Gabry Ponte", "KEL"],
        "title": ["Tarantella"],
        "MUSIC_AGENT_CANONICAL_IDENTITY_VERIFIED": ["true"],
        "MUSIC_AGENT_METADATA_AUTHORITY": ["validated_provider"],
        "MUSIC_AGENT_SOURCE_PROVIDER": ["youtube"],
        "MUSIC_AGENT_SOURCE_UPLOADER": ["Unrelated uploader"],
        "MUSICBRAINZ_TRACKID": ["11111111-1111-1111-1111-111111111111"],
        "MUSICBRAINZ_ALBUMID": ["22222222-2222-2222-2222-222222222222"],
        "MUSICBRAINZ_RELEASEGROUPID": ["33333333-3333-3333-3333-333333333333"],
    }
    parsed = _read_tags(values, values)
    assert parsed["artist"] == "Gabry Ponte, KEL"
    assert parsed["canonical_identity_verified"] is False
    assert parsed["recording_mbid"] is None
    assert parsed["release_mbid"] is None
    assert parsed["release_group_mbid"] is None
    assert parsed["source_uploader"] == "Unrelated uploader"


def test_tag_mapping_handles_invalid_optional_provenance_without_type_errors():
    tags = coerce_media_tags(
        {
            "title": "Title",
            "artist": "Artist",
            "canonical_identity_verified": {"untrusted": True},
            "metadata_provenance": ["untrusted"],
        }
    )
    assert tags.canonical_identity_verified is None
    assert tags.metadata_provenance == {}


def test_real_provider_pipeline_reason_survives_tag_coercion_and_library_read():
    processor = object.__new__(DownloadJobProcessor)
    values = processor._apply_provider_metadata(
        {"artist": "Gabry Ponte, KEL", "title": "Tarantella"},
        {"metadata_authority": "direct_user_source", "reason_code": "no_candidates"},
        lease=None,
    )
    tags = coerce_media_tags(values)
    encoded = encoded_provenance(tags.metadata_provenance)
    parsed = _read_tags(
        {
            "MUSIC_AGENT_METADATA_PROVENANCE": [encoded],
            "MUSIC_AGENT_CANONICAL_IDENTITY_VERIFIED": ["false"],
            "MUSIC_AGENT_METADATA_AUTHORITY": ["direct_user_source"],
        },
        {},
    )
    assert parsed["metadata_provenance"]["canonical_metadata_resolution"] == {
        "source": "direct_user_source",
        "automatic_association": True,
        "decided_by": "deterministic",
        "reason_code": "no_candidates",
    }


def test_provider_fallback_repairs_only_non_explicit_release_inferred_version():
    processor = object.__new__(DownloadJobProcessor)
    values = processor._apply_provider_metadata(
        {
            "artist": "Gabry Ponte & KEL",
            "title": "Tarantella",
            "version_signature": "live",
            "metadata_provenance": {
                "request_constraints": {"version_constraint_explicit": False},
                "recording_version": {
                    "signature": "live",
                    "source": "release_metadata",
                },
            },
        },
        {
            "artist": "Gabry Ponte & KEL",
            "title": "Tarantella",
            "version": "studio",
            "metadata_authority": "direct_user_source",
            "reason_code": "no_candidates",
        },
        lease=None,
    )

    assert values["version_signature"] == "studio"
    assert values["metadata_provenance"]["canonical_metadata_resolution"]["reason_codes"] == [
        "inferred_version_corrected_to_studio"
    ]


def test_provider_metadata_preserves_provider_evidenced_live_with_plain_title():
    processor = object.__new__(DownloadJobProcessor)
    values = processor._apply_provider_metadata(
        {
            "artist": "Coldplay",
            "title": "Yellow",
            "version_signature": "live",
            "metadata_provenance": {
                "request_constraints": {"version_constraint_explicit": False},
                "recording_version": {
                    "signature": "live",
                    "source": "provider_recording_metadata",
                },
            },
        },
        {
            "artist": "Coldplay",
            "title": "Yellow",
            "version": "live",
            "metadata_authority": "direct_user_source",
            "reason_code": "no_candidates",
        },
        lease=None,
    )

    assert values["version_signature"] == "live"
    assert "reason_codes" not in values["metadata_provenance"]["canonical_metadata_resolution"]


def test_nested_resolution_is_allowlisted_bounded_and_not_recursively_copied():
    nested = {
        "source": "direct_user_source",
        "automatic_association": False,
        "reason_code": "no_candidates",
        "decided_by": "user",
        "headers": {"authorization": "do not retain"},
        "raw_prompt": "do not retain",
        "canonical_metadata_resolution": {"reason_code": "do not recurse"},
    }
    provenance = {"canonical_metadata_resolution": nested, "prompt_version": "📀" * 160}
    encoded = encoded_provenance(provenance)
    assert encoded is not None and len(encoded.encode("utf-8")) <= MAX_PROVENANCE_BYTES
    decoded = sanitized_provenance(encoded)
    assert decoded["canonical_metadata_resolution"] == {
        "source": "direct_user_source",
        "automatic_association": False,
        "reason_code": "no_candidates",
        "decided_by": "user",
    }
    assert encoded_provenance(decoded) == encoded


def test_nested_resolution_budget_includes_wrapper_and_top_level_fields():
    nested = {
        key: "📀" * 160
        for key in (
            "decided_by",
            "decision_id",
            "decision_fingerprint",
            "prompt_version",
            "recording_candidate_id",
            "release_candidate_id",
            "source_candidate_id",
        )
    }
    nested["reason_code"] = "no_candidates"
    encoded = encoded_provenance({"canonical_metadata_resolution": nested, **nested})
    assert encoded is not None and len(encoded.encode("utf-8")) <= MAX_PROVENANCE_BYTES
    assert sanitized_provenance(encoded) == json.loads(encoded)
