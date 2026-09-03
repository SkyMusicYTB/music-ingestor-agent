from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import BackgroundSession
from app.services.security import SESSION_COOKIE

router = APIRouter(tags=["events"])
_MAX_EVENT_ID = 9_223_372_036_854_775_807


@router.get("/api/v1/events")
async def events(
    request: Request,
    authenticated: BackgroundSession,
    after: int | None = Query(default=None, ge=0, le=_MAX_EVENT_ID),
) -> StreamingResponse:
    last_id = _initial_event_id(request.headers.get("last-event-id"), after)

    return StreamingResponse(
        _event_stream(request, last_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _event_stream(request: Request, last_id: int | None) -> AsyncGenerator[str, None]:
    minimum, maximum = request.app.state.events.bounds()
    # Authenticated HTML pages capture `after` before reading rendered state. A
    # client without that cursor starts at the current tail for compatibility.
    if last_id is None:
        last_id = maximum or 0
    if last_id and minimum is not None and last_id < minimum - 1:
        payload = json.dumps({"reason": "event history expired"}, separators=(",", ":"))
        yield f"event: reset\ndata: {payload}\n\n"
        last_id = maximum or 0
    quiet_ticks = 0
    while True:
        if await request.is_disconnected():
            return
        # SSE is not user activity: it must neither extend idle sessions nor
        # retain privileges after a reset, role change or forced change.
        authenticated = request.app.state.auth.resolve_session(
            request.cookies.get(SESSION_COOKIE), touch=False
        )
        if authenticated is None or authenticated.must_change_password:
            yield "event: signed_out\ndata: {}\n\n"
            return
        _minimum, maximum = request.app.state.events.bounds()
        through = maximum or 0
        rows = request.app.state.events.visible_after(
            last_id,
            user_id=authenticated.user_id,
            is_admin=authenticated.role == "admin",
            through=through,
        )
        if rows:
            quiet_ticks = 0
            for item in rows:
                data = json.dumps(
                    {
                        "entity_type": item.entity_type,
                        "entity_id": item.entity_id,
                        "type": item.event_type,
                        "message": item.message,
                        "details": json.loads(item.details_json or "{}"),
                        "created_at": item.created_at.isoformat(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {item.id}\nevent: update\ndata: {data}\n\n"
                last_id = item.id
            # Never jump beyond an authorized full batch: more visible rows
            # may exist before the captured global tail.
            if len(rows) < 100 and through > last_id:
                last_id = through
                yield f"id: {last_id}\nevent: checkpoint\ndata: {{}}\n\n"
        else:
            if through > last_id:
                last_id = through
                yield f"id: {last_id}\nevent: checkpoint\ndata: {{}}\n\n"
            quiet_ticks += 1
            if quiet_ticks >= 15:
                yield ": heartbeat\n\n"
                quiet_ticks = 0
        await asyncio.sleep(1)


def _last_event_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        event_id = int(value)
    except ValueError:
        return None
    # SQLite binds signed 64-bit integers. Malformed/oversized header cursors
    # fall back to the validated page cursor, just like other invalid headers.
    return max(0, event_id) if event_id <= _MAX_EVENT_ID else None


def _initial_event_id(header_value: str | None, after: int | None) -> int | None:
    header_id = _last_event_id(header_value)
    return header_id if header_id is not None else after
