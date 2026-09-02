from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import Event, Request, RequestTrack
from app.schemas import MusicProposal
from app.services.duplicates import (
    DuplicateCandidate,
    DuplicateDetector,
    normalize_text,
    version_signature,
    versions_compatible,
)
from app.services.request_constraints import parse_explicit_request_constraints


class ProposalLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedMetadata:
    recording_mbid: str
    artist: str
    title: str
    album: str | None
    duration_seconds: float | None
    version_signature: str
    release_mbid: str | None
    release_group_mbid: str | None
    score: float
    decided_by: str = "deterministic"
    model_confidence: float | None = None
    openai_call_id: str | None = None


class ProposalService:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._duplicates = DuplicateDetector(settings.music_path)
        self._max_candidates = settings.max_candidates_per_request

    def store(
        self,
        request_id: str,
        proposal: MusicProposal,
        *,
        status_override: str | None = None,
        expected_lease_token: str | None = None,
        warning_code: str | None = None,
        verified_metadata: Mapping[str, VerifiedMetadata] | None = None,
    ) -> list[str]:
        if len(proposal.tracks) > self._max_candidates:
            raise ValueError("proposal exceeds the configured candidate limit")
        with self._session_factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise LookupError(request_id)
            if expected_lease_token is not None and request.lease_token != expected_lease_token:
                raise ProposalLeaseLost(request_id)
            request_constraints = parse_explicit_request_constraints(
                request.raw_text,
                input_kind=request.input_kind,
            )
            session.execute(delete(RequestTrack).where(RequestTrack.request_id == request_id))
            identifiers: list[str] = []
            selected_count = 0
            warning_count = 0
            for ordinal, item in enumerate(proposal.tracks, start=1):
                signature = request_constraints.version or version_signature(
                    item.version, item.title, item.album
                )
                verified = _verified_match(item, signature, verified_metadata or {})
                recording_mbid = verified.recording_mbid if verified is not None else None
                release_mbid = verified.release_mbid if verified is not None else None
                release_group_mbid = verified.release_group_mbid if verified is not None else None
                duplicate = self._duplicates.find(
                    session,
                    DuplicateCandidate(
                        artist=item.artist,
                        title=item.title,
                        version_signature=signature,
                        duration_seconds=item.duration_seconds,
                        recording_mbid=recording_mbid,
                    ),
                )
                selected = duplicate.status != "owned"
                selected_count += int(selected)
                warning_count += int(duplicate.status == "possible")
                display_evidence = [str(value) for value in item.evidence]
                if item.source_url is not None:
                    display_evidence.append(str(item.source_url))
                display_evidence = list(dict.fromkeys(display_evidence))
                track = RequestTrack(
                    request_id=request_id,
                    ordinal=ordinal,
                    artist=item.artist,
                    title=item.title,
                    album=item.album,
                    album_artist=item.album_artist,
                    year=item.year,
                    duration_seconds=item.duration_seconds,
                    recording_mbid=recording_mbid,
                    release_mbid=release_mbid,
                    release_group_mbid=release_group_mbid,
                    suggested_recording_mbid=(item.recording_mbid if verified is None else None),
                    suggested_release_mbid=(item.release_mbid if verified is None else None),
                    suggested_release_group_mbid=(
                        item.release_group_mbid if verified is None else None
                    ),
                    canonical_identity_verified=verified is not None,
                    # Model-returned URLs are evidence only. Executable source URLs are
                    # accepted exclusively from a worker-owned, policy-validated candidate.
                    source_url=None,
                    version_signature=signature,
                    rationale=item.rationale,
                    evidence_json=json.dumps(display_evidence, ensure_ascii=False),
                    duplicate_status=duplicate.status,
                    duplicate_track_id=duplicate.track_id,
                    selected=selected,
                    metadata_confidence=(
                        verified.model_confidence
                        if verified is not None and verified.model_confidence is not None
                        else verified.score / 100.0
                        if verified is not None
                        else None
                    ),
                    metadata_provenance_json=json.dumps(
                        {
                            "automatic_association": verified is not None,
                            # Model-proposed album/version text remains descriptive.
                            # Only constraints recovered from the user's own request
                            # are allowed to influence release/source precedence.
                            "album_constraint_explicit": request_constraints.album is not None,
                            "request_constraints": request_constraints.as_provenance(),
                            "source": (
                                "openai_canonical_match"
                                if verified is not None and verified.decided_by == "openai"
                                else "musicbrainz_search_recordings"
                                if verified is not None
                                else "unverified_model_output"
                            ),
                            "recording_mbid": recording_mbid,
                            "release_mbid": release_mbid,
                            "suggested_recording_mbid": (
                                item.recording_mbid if verified is None else None
                            ),
                            "suggested_release_mbid": (
                                item.release_mbid if verified is None else None
                            ),
                            "score": verified.score if verified is not None else None,
                            "model_confidence": (
                                verified.model_confidence if verified is not None else None
                            ),
                            "openai_call_id": (
                                verified.openai_call_id if verified is not None else None
                            ),
                            "decided_by": verified.decided_by if verified is not None else None,
                        },
                        separators=(",", ":"),
                    ),
                )
                session.add(track)
                session.flush()
                identifiers.append(track.id)
            request.discovered_count = len(identifiers)
            request.selected_count = selected_count
            request.warning_count = warning_count
            request.status = status_override or _proposal_status(proposal)
            request.error_code = warning_code[:80] if warning_code else None
            request.error_message = (
                "Discovery completed with a provider fallback limitation." if warning_code else None
            )
            request.lease_token = None
            request.lease_expires_at = None
            session.add(
                Event(
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.proposal",
                    message=(
                        "Clarification required"
                        if proposal.clarification
                        else "Proposal ready for review"
                        if identifiers
                        else "Discovery completed without candidates"
                    ),
                    details_json=json.dumps(
                        {
                            "count": len(identifiers),
                            "selected_count": selected_count,
                            "warning_count": warning_count,
                            "exhausted": proposal.exhausted,
                            "summary": proposal.summary[:1000],
                            "clarification": proposal.clarification,
                            "warning_code": warning_code,
                        },
                        separators=(",", ":"),
                    ),
                )
            )
            return identifiers


def _proposal_status(proposal: MusicProposal) -> str:
    if proposal.clarification:
        return "needs_clarification"
    if proposal.tracks:
        return "preview"
    return "incomplete"


def _verified_match(
    item: object,
    signature: str,
    verified_metadata: Mapping[str, VerifiedMetadata],
) -> VerifiedMetadata | None:
    recording_mbid = getattr(item, "recording_mbid", None)
    if not isinstance(recording_mbid, str):
        return None
    verified = verified_metadata.get(recording_mbid)
    if verified is None:
        return None
    if normalize_text(str(getattr(item, "artist", ""))) != normalize_text(verified.artist):
        return None
    if normalize_text(str(getattr(item, "title", ""))) != normalize_text(verified.title):
        return None
    if not versions_compatible(signature, verified.version_signature):
        return None
    proposed_duration = getattr(item, "duration_seconds", None)
    if (
        isinstance(proposed_duration, (int, float))
        and verified.duration_seconds is not None
        and abs(float(proposed_duration) - verified.duration_seconds)
        > max(4.0, verified.duration_seconds * 0.02)
    ):
        return None
    return verified
