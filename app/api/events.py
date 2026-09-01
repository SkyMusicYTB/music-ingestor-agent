from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import CurrentSession

router = APIRouter(tags=["events"])


@router.get("/api/v1/events")
async def events(
    request: Request,
    authenticated: CurrentSession,
    after: int | None = Query(default=None, ge=0, le=9_223_372_036_854_775_807),
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
        rows = request.app.state.events.after(last_id)
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
        else:
            quiet_ticks += 1
            if quiet_ticks >= 15:
                yield ": heartbeat\n\n"
                quiet_ticks = 0
        await asyncio.sleep(1)


def _last_event_id(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _initial_event_id(header_value: str | None, after: int | None) -> int | None:
    header_id = _last_event_id(header_value)
    return header_id if header_id is not None else after
