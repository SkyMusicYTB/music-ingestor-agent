from __future__ import annotations

import json
import math
from itertools import islice

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import DownloadJob, Event, Request, RequestTrack
from app.services.security import safe_event_text


def _safe_details(value: object, depth: int = 0) -> object:
    """Bound input before JSON encoding; reject secret-bearing keys centrally."""
    if depth > 4:
        return "[truncated]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(1 << 63) <= value < (1 << 63) else "[out of range]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_event_text(value[:500])
    if isinstance(value, (list, tuple)):
        return [_safe_details(item, depth + 1) for item in value[:40]]
    if isinstance(value, dict):
        return {
            str(key)[:64]: _safe_details(item, depth + 1)
            for key, item in islice(value.items(), 40)
            if isinstance(key, str)
            and not any(
                secret in key.casefold()
                for secret in ("password", "cookie", "token", "authorization", "secret", "api_key")
            )
        }
    return None


def make_event(
    session: Session,
    *,
    entity_type: str,
    event_type: str,
    message: str,
    entity_id: str | None = None,
    details: dict[str, object] | None = None,
    details_json: str = "{}",
    audience: str | None = None,
    user_id: str | None = None,
) -> Event:
    """Build a bounded event in its caller's transaction; default to deny-by-default."""
    if audience is None:
        if entity_type == "request" and entity_id:
            user_id = session.scalar(select(Request.user_id).where(Request.id == entity_id))
        elif entity_type == "job" and entity_id:
            user_id = session.scalar(
                select(Request.user_id)
                .join(RequestTrack)
                .join(DownloadJob)
                .where(DownloadJob.id == entity_id)
            )
        audience = "user" if user_id else "admin"
    if audience not in {"user", "admin", "all_authenticated"}:
        raise ValueError("invalid event audience")
    if (audience == "user") != (user_id is not None):
        raise ValueError("user events require exactly one owner")
    if details is None:
        try:
            decoded = json.loads(details_json) if len(details_json) <= 16_384 else {}
        except (TypeError, ValueError):
            decoded = {}
        details = decoded if isinstance(decoded, dict) else {}
    if audience == "all_authenticated":
        if entity_type != "library" or event_type not in {
            "library.scan_completed",
            "library.updated",
        }:
            raise ValueError("this event is not eligible for shared delivery")
        details = {
            key: value
            for key, value in details.items()
            if key in {"scanned", "changed", "missing", "errors", "indexed"}
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        }
        message = "Shared library updated"
        entity_id = None
    encoded = json.dumps(
        _safe_details(details), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(encoded.encode()) > 4000:
        encoded = '{"details_truncated":true}'
    return Event(
        entity_type=entity_type[:32],
        entity_id=entity_id,
        event_type=event_type[:64],
        message=safe_event_text(message),
        details_json=encoded,
        audience=audience,
        user_id=user_id,
    )


def audience_predicate(user_id: str, is_admin: bool) -> ColumnElement[bool]:
    visible = [
        and_(Event.audience == "user", Event.user_id == user_id),
        Event.audience == "all_authenticated",
    ]
    if is_admin:
        visible.append(Event.audience == "admin")
    return or_(*visible)


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
        with self.factory.begin() as session:
            event = make_event(
                session,
                entity_type=entity_type,
                event_type=event_type,
                message=message,
                entity_id=entity_id,
                details=details,
            )
            session.add(event)
            session.flush()
            return event.id

    def visible_after(
        self,
        event_id: int,
        *,
        user_id: str,
        is_admin: bool,
        through: int,
        limit: int = 100,
    ) -> list[Event]:
        with self.factory() as session:
            return list(
                session.scalars(
                    select(Event)
                    .where(
                        Event.id > event_id,
                        Event.id <= through,
                        audience_predicate(user_id, is_admin),
                    )
                    .order_by(Event.id)
                    .limit(min(max(limit, 1), 500))
                )
            )

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
