from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Event
from app.services.security import safe_event_text


class EventRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory

    def emit(
        self,
        entity_type: str,
        event_type: str,
        message: str,
        entity_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> int:
        event = Event(
            entity_type=entity_type[:32],
            entity_id=entity_id,
            event_type=event_type[:64],
            message=safe_event_text(message),
            details_json=json.dumps(details or {}, ensure_ascii=False, separators=(",", ":"))[
                :4000
            ],
        )
        with self.factory.begin() as session:
            session.add(event)
            session.flush()
            return event.id

    def after(self, event_id: int, limit: int = 100) -> list[Event]:
        with self.factory() as session:
            return list(
                session.scalars(
                    select(Event)
                    .where(Event.id > event_id)
                    .order_by(Event.id)
                    .limit(min(limit, 500))
                )
            )

    def bounds(self) -> tuple[int | None, int | None]:
        with self.factory() as session:
            row = session.execute(select(func.min(Event.id), func.max(Event.id))).one()
            return row[0], row[1]
