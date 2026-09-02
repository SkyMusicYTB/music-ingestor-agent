from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import CurrentSession
from app.db.models import OpenAICall

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])


def _aggregate(
    factory: sessionmaker[Session], *, period_format: str, limit: int
) -> list[dict[str, object]]:
    with factory() as session:
        period = func.strftime(period_format, OpenAICall.created_at)
        accounted_count = func.sum(
            case(
                (
                    or_(OpenAICall.status == "completed", OpenAICall.total_tokens > 0),
                    1,
                ),
                else_=0,
            )
        )
        priced_count = func.count(OpenAICall.estimated_cost_microusd)
        cost = case(
            (accounted_count == priced_count, func.sum(OpenAICall.estimated_cost_microusd)),
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
        "recent": _recent_calls(factory, limit=200),
    }


def _recent_calls(factory: sessionmaker[Session], *, limit: int) -> list[dict[str, object]]:
    """Return only bounded, explicitly allowlisted operator diagnostics."""

    with factory() as session:
        rows = list(
            session.scalars(select(OpenAICall).order_by(OpenAICall.created_at.desc()).limit(limit))
        )
    return [
        {
            "application_call_id": row.application_call_id or row.id,
            "request_id": row.request_id,
            "response_id": row.response_id,
            "provider_request_id": row.provider_request_id,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "status": row.status,
            "error_code": row.error_code,
            "error_category": _friendly_error_category(row.error_code),
            "exception_class": row.exception_class,
            "http_status": row.http_status,
            "provider_error_code": row.provider_error_code,
            "provider_error_parameter": row.provider_error_parameter,
            "failure_phase": row.failure_phase,
            "retryable": row.retryable,
            "latency_ms": row.latency_ms,
            "service_tier": row.service_tier,
            "total_tokens": row.total_tokens,
            "estimated_cost_microusd": row.estimated_cost_microusd,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _friendly_error_category(error_code: str | None) -> str | None:
    if error_code is None:
        return None
    if "rate_limit" in error_code:
        return "Rate limited"
    if "refusal" in error_code or "rejected" in error_code:
        return "Rejected request"
    if "timeout" in error_code:
        return "Timeout"
    if "malformed" in error_code or "unexpected_tool" in error_code:
        return "Malformed response"
    if "model" in error_code or "unsupported" in error_code:
        return "Model unavailable"
    return "Temporary provider failure"


@router.get("")
def usage(request: Request, authenticated: CurrentSession) -> dict[str, object]:
    return usage_snapshot(request.app.state.session_factory)
