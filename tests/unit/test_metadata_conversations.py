from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Conversation, Request, User
from app.services.conversations import ConversationService


def database() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_context_keeps_latest_three_and_compacts_older_turns() -> None:
    factory = database()
    with factory.begin() as session:
        user = User(
            username="listener",
            username_normalized="listener",
            password_hash="test-hash",  # noqa: S106 - inert fixture value
        )
        session.add(user)
        session.flush()
        conversation = Conversation(
            user_id=user.id,
            title="Long conversation",
            constraints_json=json.dumps({"exclude": ["live"]}),
            turn_count=12,
        )
        session.add(conversation)
        session.flush()
        request_ids: list[str] = []
        for number in range(12):
            item = Request(
                user_id=user.id,
                conversation_id=conversation.id,
                raw_text=f"turn {number}",
                action="add" if number % 2 else "find",
                input_kind="natural_language",
                requested_count=number if number else None,
                status="preview" if number < 11 else "pending",
                idempotency_key=f"conversation-turn-{number}",
            )
            session.add(item)
            session.flush()
            request_ids.append(item.id)

    context = ConversationService(factory).orchestration_context(request_ids[-1])

    assert [turn.text for turn in context.prior_turns] == ["turn 8", "turn 9", "turn 10"]
    assert context.constraints["exclude"] == ["live"]
    compacted = context.constraints["history_compaction"]
    assert compacted["version"] == 1
    assert compacted["folded_turn_count"] == 8
    assert [item["text"] for item in compacted["recent_folded_directives"]] == [
        f"turn {number}" for number in range(8)
    ]
    assert sum(compacted["action_counts"].values()) == 8

    with factory() as session:
        stored = session.get(Conversation, conversation.id)
        assert stored is not None
        persisted = json.loads(stored.constraints_json)
    assert persisted["history_compaction"] == compacted

    # Recomputing is deterministic and does not append or grow a free-form summary.
    repeated = ConversationService(factory).orchestration_context(request_ids[-1])
    assert repeated.constraints["history_compaction"] == compacted
