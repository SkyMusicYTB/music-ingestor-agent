from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import Settings
from app.db.models import Request, RequestTrack


@dataclass(frozen=True)
class ConfirmationDecision:
    auto_queue: bool
    reason: str


def confirmation_decision(
    request: Request, tracks: list[RequestTrack], settings: Settings
) -> ConfirmationDecision:
    if request.action != "add":
        return ConfirmationDecision(False, "find requests always require preview")
    if not settings.auto_download_exact_single:
        return ConfirmationDecision(False, "automatic exact-track acquisition is disabled")
    selected = [track for track in tracks if track.selected]
    if len(selected) != 1 or len(tracks) != 1:
        return ConfirmationDecision(False, "multiple or competing candidates require approval")
    track = selected[0]
    if track.duplicate_status != "none":
        return ConfirmationDecision(False, "duplicate decisions require review")
    exact_direct_source = bool(
        request.input_kind == "youtube_url" and track.source_extractor and track.source_id
    )
    if not exact_direct_source:
        if (track.metadata_confidence or 0.0) < 0.88:
            return ConfirmationDecision(
                False, "metadata confidence is below the exact-match threshold"
            )
        if not _has_verified_automatic_association(track):
            return ConfirmationDecision(
                False, "canonical identity was not verified by the deterministic matcher"
            )
    if not (track.recording_mbid or (track.source_extractor and track.source_id)):
        return ConfirmationDecision(False, "candidate lacks an exact canonical or source identity")
    if request.requested_count not in (None, 1):
        return ConfirmationDecision(False, "bulk requests require approval")
    return ConfirmationDecision(True, "single exact high-confidence Add request")


def _has_verified_automatic_association(track: RequestTrack) -> bool:
    try:
        provenance = json.loads(track.metadata_provenance_json or "{}")
    except (AttributeError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(provenance, dict):
        return False
    score = provenance.get("score")
    return bool(
        provenance.get("automatic_association") is True
        and provenance.get("source") == "musicbrainz_search_recordings"
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
        and score >= 88
        and provenance.get("recording_mbid") == track.recording_mbid
    )
