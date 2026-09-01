from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import CurrentSession
from app.db.models import OpenAICall

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


def _aggregate(
    factory: sessionmaker[Session], *, period_format: str, limit: int
) -> list[dict[str, object]]:
    with factory() as session:
        period = func.strftime(period_format, OpenAICall.created_at)
        completed_count = func.sum(case((OpenAICall.status == "completed", 1), else_=0))
        priced_count = func.count(OpenAICall.estimated_cost_microusd)
        cost = case(
            (completed_count == priced_count, func.sum(OpenAICall.estimated_cost_microusd)),
            else_=None,
        )
        rows = session.execute(
            select(
                period.label("period"),
                func.count(OpenAICall.id),
                func.sum(OpenAICall.input_tokens),
                func.sum(OpenAICall.cached_input_tokens),
                func.sum(OpenAICall.cache_write_tokens),
                func.sum(OpenAICall.output_tokens),
                func.sum(OpenAICall.reasoning_tokens),
                func.sum(OpenAICall.total_tokens),
                func.sum(OpenAICall.web_search_count),
                cost,
            )
            .group_by(period)
            .order_by(period.desc())
            .limit(limit)
        ).all()
    return [
        {
            "period": row[0],
            "calls": row[1],
            "input_tokens": row[2] or 0,
            "cached_input_tokens": row[3] or 0,
            "cache_write_tokens": row[4] or 0,
            "output_tokens": row[5] or 0,
            "reasoning_tokens": row[6] or 0,
            "total_tokens": row[7] or 0,
            "web_searches": row[8] or 0,
            "estimated_cost_microusd": row[9],
        }
        for row in rows
    ]


def usage_snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    return {
        "daily": _aggregate(factory, period_format="%Y-%m-%d", limit=90),
        "monthly": _aggregate(factory, period_format="%Y-%m", limit=36),
    }


@router.get("")
def usage(request: Request, authenticated: CurrentSession) -> dict[str, object]:
    return usage_snapshot(request.app.state.session_factory)
