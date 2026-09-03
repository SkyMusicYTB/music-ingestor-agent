from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import case, func, select, true
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import CurrentSession
from app.db.models import OpenAICall

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])

_TERMINATION_REASONS = frozenset(
    {
        "normal_synthesis",
        "forced_final_synthesis",
        "no_progress_synthesis",
        "model_round_exhaustion",
        "wall_time_exhaustion",
        "provider_failure",
        "refused",
        "malformed_response",
        "cancelled",
        "lease_lost",
    }
)


def latest_execution_summary(factory: sessionmaker[Session]) -> dict[str, object] | None:
    """Admin-only callers: one completed execution, never request/provider content."""
    with factory() as session:
        row = session.execute(
            select(
                OpenAICall.created_at,
                OpenAICall.model_round,
                OpenAICall.configured_model_rounds,
                OpenAICall.configured_tool_calls,
                OpenAICall.configured_agent_seconds,
                OpenAICall.termination_reason,
            )
            .where(
                OpenAICall.model_round.is_not(None),
                OpenAICall.termination_reason.is_not(None),
            )
            .order_by(OpenAICall.created_at.desc(), OpenAICall.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None

    def budget(value: object, minimum: int = 1, maximum: int = 50) -> int | None:
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and minimum <= value <= maximum
            else None
        )

    return {
        "recorded_at": row[0].isoformat(),
        "model_rounds_used": budget(row[1]),
        "configured_model_rounds": budget(row[2]),
        "configured_tool_calls": budget(row[3]),
        "configured_agent_seconds": budget(row[4], minimum=10, maximum=600),
        "termination_reason": row[5] if row[5] in _TERMINATION_REASONS else "unknown",
    }


def _aggregate(
    factory: sessionmaker[Session],
    *,
    period_format: str,
    limit: int,
    user_id: str | None = None,
    scope: str = "all",
) -> list[dict[str, object]]:
    with factory() as session:
        period = func.strftime(period_format, OpenAICall.created_at)
        accounted_count = func.sum(
            case(
                (
                    OpenAICall.usage_reported.is_(True),
                    1,
                ),
                else_=0,
            )
        )
        priced_count = func.count(OpenAICall.estimated_cost_microusd)
        cost = case(
            (
                (accounted_count == priced_count) & (accounted_count == func.count(OpenAICall.id)),
                func.sum(OpenAICall.estimated_cost_microusd),
            ),
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
                (func.count(OpenAICall.id) - accounted_count),
            )
            .where(_scope(user_id, scope))
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
            "unknown_usage_calls": row[10],
        }
        for row in rows
    ]


def _scope(user_id: str | None, scope: str):  # type: ignore[no-untyped-def]
    if scope == "own":
        if user_id is None:
            raise ValueError("usage owner is required")
        return OpenAICall.owner_user_id == user_id
    if scope == "system":
        return OpenAICall.owner_user_id.is_(None)
    if scope != "all":
        raise ValueError("invalid usage scope")
    return true()


def usage_snapshot(
    factory: sessionmaker[Session], *, user_id: str | None = None, scope: str = "all"
) -> dict[str, object]:
    return {
        "scope": scope,
        "daily": _aggregate(
            factory, period_format="%Y-%m-%d", limit=90, user_id=user_id, scope=scope
        ),
        "monthly": _aggregate(
            factory, period_format="%Y-%m", limit=36, user_id=user_id, scope=scope
        ),
        "recent": _recent_calls(factory, limit=200, user_id=user_id, scope=scope),
    }


def _recent_calls(
    factory: sessionmaker[Session], *, limit: int, user_id: str | None = None, scope: str = "all"
) -> list[dict[str, object]]:
    """Return only bounded, explicitly allowlisted operator diagnostics."""

    with factory() as session:
        rows = list(
            session.scalars(
                select(OpenAICall)
                .where(_scope(user_id, scope))
                .order_by(OpenAICall.created_at.desc())
                .limit(limit)
            )
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
            "owner_user_id": row.owner_user_id,
            "usage_reported": row.usage_reported,
            "phase": row.phase,
            "model_round": row.model_round,
            "configured_model_rounds": row.configured_model_rounds,
            "configured_tool_calls": row.configured_tool_calls,
            "configured_agent_seconds": row.configured_agent_seconds,
            "termination_reason": row.termination_reason,
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
def usage(
    request: Request, authenticated: CurrentSession, scope: Literal["own", "all", "system"] = "own"
) -> dict[str, object]:
    if scope != "own" and authenticated.role != "admin":
        raise HTTPException(403, "administrator access required")
    return usage_snapshot(
        request.app.state.session_factory, user_id=authenticated.user_id, scope=scope
    )


@router.get("/by-user")
def usage_by_user(request: Request, authenticated: CurrentSession) -> dict[str, object]:
    if authenticated.role != "admin":
        raise HTTPException(403, "administrator access required")
    with request.app.state.session_factory() as session:
        rows = session.execute(
            select(
                OpenAICall.owner_user_id,
                func.count(OpenAICall.id),
                func.sum(OpenAICall.total_tokens),
                func.sum(case((OpenAICall.usage_reported.is_(True), 0), else_=1)),
            )
            .group_by(OpenAICall.owner_user_id)
            .order_by(OpenAICall.owner_user_id)
        ).all()
    return {
        "groups": [
            {
                "owner_user_id": row[0],
                "calls": row[1],
                "known_total_tokens": row[2],
                "unknown_usage_calls": row[3],
            }
            for row in rows
        ]
    }
