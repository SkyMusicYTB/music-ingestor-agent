from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import is_curated_collection_url
from app.db.models import Conversation, Request, RequestTrack, ServiceTask
from app.repositories.events import make_event
from app.schemas import MusicProposal
from app.services.request_constraints import parse_explicit_request_constraints
from app.sources import ProviderIdentity, ProviderURLPolicy, provider_for_url

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
DIRECT_SINGLE_INPUT_KINDS = frozenset({"youtube_url", "media_url"})
DIRECT_COLLECTION_INPUT_KIND = "media_collection_url"
_COUNT_NOUN = r"(?:songs?|tracks?|recordings?)"
_NUMERIC_COUNT = re.compile(
    rf"\b(?P<count>\d{{1,3}})\s*(?:[-\u2013\u2014]\s*)?{_COUNT_NOUN}\b", re.I
)
_ACTION_COUNT = re.compile(
    r"\b(?:find|add|download|get|suggest|recommend)\s+(?:me\s+)?(?P<count>\d{1,3})\b",
    re.I,
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_WORD_COUNT = re.compile(
    rf"\b(?P<count>{'|'.join(_NUMBER_WORDS)})\s+{_COUNT_NOUN}\b",
    re.I,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def classify_input(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return "natural_language"
    if parsed.scheme or parsed.netloc:
        provider = provider_for_url(value.strip())
        if provider not in {
            ProviderIdentity.YOUTUBE,
            ProviderIdentity.SOUNDCLOUD,
            ProviderIdentity.BANDCAMP,
        }:
            raise ValueError("direct URLs are limited to reviewed curated media providers")
        validation = ProviderURLPolicy().validate(value.strip(), provider=provider)
        if not validation.allowed:
            raise ValueError("direct media URL does not satisfy the source policy")
        if is_curated_collection_url(value.strip()):
            return DIRECT_COLLECTION_INPUT_KIND
        return "youtube_url" if provider is ProviderIdentity.YOUTUBE else "media_url"
    return "natural_language"


def parse_requested_count(value: str, *, input_kind: str) -> int | None:
    if input_kind in DIRECT_SINGLE_INPUT_KINDS:
        return 1
    if input_kind == DIRECT_COLLECTION_INPUT_KIND:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    match = _NUMERIC_COUNT.search(normalized) or _ACTION_COUNT.search(normalized)
    count: int | None = int(match.group("count")) if match else None
    if count is None:
        word_match = _WORD_COUNT.search(normalized)
        if word_match:
            count = _NUMBER_WORDS[word_match.group("count").casefold()]
    if count is not None and not 1 <= count <= 250:
        raise ValueError("requested count must be between 1 and 250")
    return count


@dataclass(frozen=True)
class CreatedRequest:
    request: Request
    created: bool


class RequestRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def create(
        self,
        *,
        user_id: str,
        text: str,
        action: str,
        idempotency_key: str,
        conversation_id: str | None = None,
        refinement_parent_id: str | None = None,
    ) -> CreatedRequest:
        text = text.strip()
        try:
            with self.factory.begin() as session:
                conversation = None
                if conversation_id:
                    conversation = session.scalar(
                        select(Conversation).where(
                            Conversation.id == conversation_id,
                            Conversation.user_id == user_id,
                            Conversation.archived_at.is_(None),
                        )
                    )
                    if conversation is None:
                        raise ValueError("conversation not found")
                else:
                    conversation = Conversation(
                        user_id=user_id, title=text[:120], constraints_json="{}", turn_count=0
                    )
                    session.add(conversation)
                    session.flush()
                input_kind = classify_input(text)
                request = Request(
                    user_id=user_id,
                    conversation_id=conversation.id,
                    refinement_parent_id=refinement_parent_id,
                    raw_text=text,
                    action=action,
                    input_kind=input_kind,
                    requested_count=parse_requested_count(text, input_kind=input_kind),
                    status="pending",
                    idempotency_key=idempotency_key,
                )
                conversation.turn_count += 1
                conversation.active_at = utc_now()
                session.add(request)
                session.flush()
                task_kind = (
                    "resolve_direct_request"
                    if request.input_kind
                    in {*DIRECT_SINGLE_INPUT_KINDS, DIRECT_COLLECTION_INPUT_KIND}
                    else "orchestrate_request"
                )
                target = "worker" if task_kind == "resolve_direct_request" else "web"
                session.add(
                    ServiceTask(
                        target=target,
                        kind=task_kind,
                        payload_json=json.dumps({"request_id": request.id}, separators=(",", ":")),
                    )
                )
                session.add(
                    make_event(
                        session,
                        entity_type="request",
                        entity_id=request.id,
                        event_type="request.created",
                        message="Request accepted",
                    )
                )
                session.flush()
                return CreatedRequest(request, True)
        except IntegrityError as error:
            with self.factory() as session:
                existing = session.scalar(
                    select(Request).where(
                        Request.user_id == user_id, Request.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise
                if existing.raw_text != text or existing.action != action:
                    raise ValueError(
                        "Idempotency-Key was already used for a different request"
                    ) from error
                return CreatedRequest(existing, False)

    def refine(
        self, *, user_id: str, parent: Request, text: str, idempotency_key: str
    ) -> CreatedRequest:
        return self.create(
            user_id=user_id,
            text=text,
            action=parent.action,
            idempotency_key=idempotency_key,
            conversation_id=parent.conversation_id,
            refinement_parent_id=parent.id,
        )

    def get_for_user(self, request_id: str, user_id: str) -> Request | None:
        with self.factory() as session:
            return session.scalar(
                select(Request).where(Request.id == request_id, Request.user_id == user_id)
            )

    def tracks_for_request(self, request_id: str) -> list[RequestTrack]:
        with self.factory() as session:
            return list(
                session.scalars(
                    select(RequestTrack)
                    .where(RequestTrack.request_id == request_id)
                    .order_by(RequestTrack.ordinal)
                )
            )

    def store_proposal(self, request_id: str, proposal: MusicProposal) -> list[str]:
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise LookupError(request_id)
            request_constraints = parse_explicit_request_constraints(
                request.raw_text,
                input_kind=request.input_kind,
            )
            session.query(RequestTrack).filter(RequestTrack.request_id == request_id).delete()
            ids: list[str] = []
            for ordinal, item in enumerate(proposal.tracks, start=1):
                display_evidence = [str(value) for value in item.evidence]
                if item.source_url is not None:
                    display_evidence.append(str(item.source_url))
                track = RequestTrack(
                    request_id=request_id,
                    ordinal=ordinal,
                    artist=item.artist,
                    title=item.title,
                    album=item.album,
                    album_artist=item.album_artist,
                    year=item.year,
                    duration_seconds=item.duration_seconds,
                    recording_mbid=None,
                    release_mbid=None,
                    release_group_mbid=None,
                    suggested_recording_mbid=item.recording_mbid,
                    suggested_release_mbid=item.release_mbid,
                    suggested_release_group_mbid=item.release_group_mbid,
                    canonical_identity_verified=False,
                    source_url=None,
                    version_signature=request_constraints.version or item.version or "studio",
                    rationale=item.rationale,
                    evidence_json=json.dumps(
                        list(dict.fromkeys(display_evidence)), ensure_ascii=False
                    ),
                    metadata_confidence=item.confidence,
                    metadata_provenance_json=json.dumps(
                        {
                            "automatic_association": False,
                            "album_constraint_explicit": request_constraints.album is not None,
                            "request_constraints": request_constraints.as_provenance(),
                            "source": "unverified_model_output",
                            "suggested_recording_mbid": item.recording_mbid,
                            "suggested_release_mbid": item.release_mbid,
                        },
                        separators=(",", ":"),
                    ),
                    selected=True,
                )
                session.add(track)
                session.flush()
                ids.append(track.id)
            request.discovered_count = len(ids)
            request.status = "needs_clarification" if proposal.clarification else "preview"
            session.add(
                make_event(
                    session,
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.proposal",
                    message="Proposal ready for review",
                    details_json=json.dumps({"count": len(ids)}),
                )
            )
            return ids
