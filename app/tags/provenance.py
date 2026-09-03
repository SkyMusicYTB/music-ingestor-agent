"""Small, allowlisted provenance stored in media comments, never provider payloads."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping

PROVIDER_AUTHORITIES = frozenset(
    {"validated_provider", "direct_user_source", "user_confirmed_provider_metadata"}
)
METADATA_AUTHORITIES = PROVIDER_AUTHORITIES | {"musicbrainz", "user_confirmed_musicbrainz"}
MAX_PROVENANCE_BYTES = 1800
MUSICBRAINZ_ID_TAG_NAMES = frozenset(
    {
        "musicbrainz_trackid",
        "musicbrainz_recordingid",
        "musicbrainz_albumid",
        "musicbrainz_releasegroupid",
        "musicbrainz track id",
        "musicbrainz album id",
        "musicbrainz release group id",
    }
)
_TEXT_FIELDS = frozenset(
    {
        "reason_code",
        "decided_by",
        "decision_id",
        "decision_fingerprint",
        "prompt_version",
        "recording_candidate_id",
        "release_candidate_id",
        "source_candidate_id",
    }
)
_SCORE_FIELDS = frozenset({"local_score", "model_confidence"})
_RESOLUTION_SOURCES = METADATA_AUTHORITIES | {
    "musicbrainz_local_candidate",
    "user_confirmed_server_candidate",
}


def bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(
        "".join(
            char
            for char in unicodedata.normalize("NFKC", value[: limit * 4])
            if not unicodedata.category(char).startswith("C")
        ).split()
    )[:limit]
    return cleaned or None


def metadata_authority(value: object) -> str | None:
    return value if isinstance(value, str) and value in METADATA_AUTHORITIES else None


def canonical_verified(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in {"true", "false"}:
        return value == "true"
    return None


PROVENANCE_TAG_FIELDS = {
    "MUSIC_AGENT_SOURCE_PROVIDER": "source_provider",
    "MUSIC_AGENT_SOURCE_UPLOADER": "source_uploader",
    "MUSIC_AGENT_CANONICAL_IDENTITY_VERIFIED": "canonical_identity_verified",
    "MUSIC_AGENT_METADATA_AUTHORITY": "metadata_authority",
    "MUSIC_AGENT_METADATA_PROVENANCE": "metadata_provenance",
}


def provenance_snapshot(values: Mapping[str, str | None]) -> dict[str, object]:
    authority = metadata_authority(values.get("metadata_authority"))
    verified = canonical_verified(values.get("canonical_identity_verified"))
    if authority in PROVIDER_AUTHORITIES:
        verified = False
    return {
        "source_provider": bounded_text(values.get("source_provider"), 40),
        "source_uploader": bounded_text(values.get("source_uploader"), 300),
        "canonical_identity_verified": verified,
        "metadata_authority": authority,
        "metadata_provenance": sanitized_provenance(values.get("metadata_provenance")),
    }


def sanitized_provenance(value: object) -> dict[str, object]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_PROVENANCE_BYTES:
            return {}
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    resolution = value.get("canonical_metadata_resolution")
    if isinstance(resolution, Mapping):
        nested: dict[str, object] = {}
        source = resolution.get("source")
        if isinstance(source, str) and source in _RESOLUTION_SOURCES:
            nested["source"] = source
        automatic = resolution.get("automatic_association")
        if isinstance(automatic, bool):
            nested["automatic_association"] = automatic
        _copy_provenance_fields(resolution, nested, byte_limit=MAX_PROVENANCE_BYTES - 40)
        if nested:
            result["canonical_metadata_resolution"] = nested
    _copy_provenance_fields(value, result)
    return result


def _copy_provenance_fields(
    value: Mapping[str, object],
    result: dict[str, object],
    *,
    byte_limit: int = MAX_PROVENANCE_BYTES,
) -> None:
    for key in sorted(_TEXT_FIELDS | _SCORE_FIELDS):
        item = value.get(key)
        if key in _TEXT_FIELDS:
            cleaned = bounded_text(item, 160)
            if cleaned is not None:
                result[key] = cleaned
        elif (
            isinstance(item, int | float)
            and not isinstance(item, bool)
            and 0 <= item <= 100
            and math.isfinite(item)
        ):
            result[key] = item
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > byte_limit:
            result.pop(key, None)


def encoded_provenance(value: object) -> str | None:
    cleaned = sanitized_provenance(value)
    return json.dumps(cleaned, ensure_ascii=False, sort_keys=True) if cleaned else None


def provenance_tag_text(tags: object, attribute: str) -> str | None:
    value = getattr(tags, attribute)
    if attribute == "canonical_identity_verified":
        return str(value).lower() if isinstance(value, bool) else None
    if attribute == "metadata_provenance":
        return encoded_provenance(value)
    return str(value) if value is not None else None
