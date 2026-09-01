from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Conversation, Request, RequestTrack


@dataclass(frozen=True, slots=True)
class PriorTurn:
    request_id: str
    text: str
    action: str
    status: str
    proposed_tracks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrchestrationContext:
    request_id: str
    conversation_id: str
    user_id: str
    text: str
    action: str
    input_kind: str
    requested_count: int | None
    prompt_version: str
    constraints: dict[str, Any]
    prior_turns: tuple[PriorTurn, ...]

    def model_input(self, *, max_candidates: int) -> dict[str, Any]:
        return {
            "request": {
                "id": self.request_id,
                "text": self.text,
                "action": self.action,
                "input_kind": self.input_kind,
                "requested_count": self.requested_count,
                "max_candidates": max_candidates,
            },
            "conversation_constraints": self.constraints,
            "prior_turns": [
                {
                    "text": turn.text,
                    "action": turn.action,
                    "status": turn.status,
                    "proposed_tracks": list(turn.proposed_tracks),
                }
                for turn in self.prior_turns
            ],
        }


class ConversationService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def orchestration_context(
        self, request_id: str, *, prior_turn_limit: int = 3
    ) -> OrchestrationContext:
        with self._session_factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise LookupError(request_id)
            conversation = session.get(Conversation, request.conversation_id)
            if conversation is None:
                raise LookupError(request.conversation_id)
            prior_condition = or_(
                Request.created_at < request.created_at,
                and_(Request.created_at == request.created_at, Request.id < request.id),
            )
            limit = max(0, min(prior_turn_limit, 3))
            prior_requests = list(
                session.scalars(
                    select(Request)
                    .where(
                        Request.conversation_id == request.conversation_id,
                        prior_condition,
                    )
                    .order_by(Request.created_at.desc(), Request.id.desc())
                    .limit(limit)
                )
            )
            turns = [self._prior_turn(session, value) for value in reversed(prior_requests)]
            constraints = _object_json(conversation.constraints_json)
            prior_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(Request)
                    .where(Request.conversation_id == request.conversation_id, prior_condition)
                )
                or 0
            )
            if prior_count + 1 >= 12:
                compacted = self._compact_history(
                    session,
                    conversation_id=request.conversation_id,
                    prior_condition=prior_condition,
                    recent_ids={item.id for item in prior_requests},
                    folded_count=max(0, prior_count - len(prior_requests)),
                )
                constraints["history_compaction"] = compacted
                conversation.constraints_json = json.dumps(
                    constraints,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            return OrchestrationContext(
                request_id=request.id,
                conversation_id=request.conversation_id,
                user_id=request.user_id,
                text=request.raw_text,
                action=request.action,
                input_kind=request.input_kind,
                requested_count=request.requested_count,
                prompt_version=request.prompt_version,
                constraints=constraints,
                prior_turns=tuple(turns),
            )

    @staticmethod
    def _compact_history(
        session: Session,
        *,
        conversation_id: str,
        prior_condition: Any,
        recent_ids: set[str],
        folded_count: int,
    ) -> dict[str, Any]:
        folded_filter = [
            Request.conversation_id == conversation_id,
            prior_condition,
        ]
        if recent_ids:
            folded_filter.append(Request.id.not_in(recent_ids))
        action_rows = session.execute(
            select(Request.action, func.count(Request.id))
            .where(*folded_filter)
            .group_by(Request.action)
            .order_by(Request.action)
        ).all()
        status_rows = session.execute(
            select(Request.status, func.count(Request.id))
            .where(*folded_filter)
            .group_by(Request.status)
            .order_by(Request.status)
        ).all()
        directives = list(
            session.scalars(
                select(Request)
                .where(*folded_filter)
                .order_by(Request.created_at.desc(), Request.id.desc())
                .limit(20)
            )
        )
        directives.reverse()
        return {
            "version": 1,
            "folded_turn_count": folded_count,
            "action_counts": {str(row[0]): int(row[1]) for row in action_rows},
            "status_counts": {str(row[0]): int(row[1]) for row in status_rows},
            "recent_folded_directives": [
                {
                    "request_id": item.id,
                    "text": item.raw_text[:500],
                    "action": item.action,
                    "input_kind": item.input_kind,
                    "requested_count": item.requested_count,
                }
                for item in directives
            ],
            "directive_limit": 20,
        }

    @staticmethod
    def _prior_turn(session: Session, request: Request) -> PriorTurn:
        tracks = list(
            session.scalars(
                select(RequestTrack)
                .where(RequestTrack.request_id == request.id)
                .order_by(RequestTrack.ordinal)
                .limit(20)
            )
        )
        return PriorTurn(
            request_id=request.id,
            text=request.raw_text[:4000],
            action=request.action,
            status=request.status,
            proposed_tracks=tuple(f"{track.artist} — {track.title}" for track in tracks),
        )


def _object_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
