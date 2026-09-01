from __future__ import annotations

from datetime import UTC, datetime

from app.api.usage import usage_snapshot
from app.db.models import OpenAICall


def _call(*, created_at: datetime, cost: int | None) -> OpenAICall:
    return OpenAICall(
        model="test-model",
        prompt_version="test-v1",
        prompt_hash="0" * 64,
        status="completed",
        input_tokens=100,
        output_tokens=50,
        reasoning_tokens=10,
        total_tokens=150,
        pricing_snapshot_json="{}",
        estimated_cost_microusd=cost,
        created_at=created_at,
    )


def test_usage_snapshot_has_daily_monthly_and_null_safe_costs(session_factory) -> None:
    with session_factory.begin() as session:
        session.add_all(
            [
                _call(created_at=datetime(2026, 8, 31, 12, tzinfo=UTC), cost=250),
                _call(created_at=datetime(2026, 9, 1, 12, tzinfo=UTC), cost=300),
                _call(created_at=datetime(2026, 9, 1, 13, tzinfo=UTC), cost=None),
            ]
        )

    snapshot = usage_snapshot(session_factory)
    daily = snapshot["daily"]
    monthly = snapshot["monthly"]
    assert isinstance(daily, list)
    assert isinstance(monthly, list)
    assert daily[0]["period"] == "2026-09-01"
    assert daily[0]["total_tokens"] == 300
    assert daily[0]["estimated_cost_microusd"] is None
    assert monthly[0]["period"] == "2026-09"
    assert monthly[0]["estimated_cost_microusd"] is None
    assert daily[1]["estimated_cost_microusd"] == 250
