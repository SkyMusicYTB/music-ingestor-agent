from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from app.api.usage import usage_snapshot
from app.db.models import OpenAICall
from app.repositories.usage import OpenAIUsageRepository, UsageValues
from app.services.orchestration import _provider_failure_details


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
    recent = snapshot["recent"]
    assert isinstance(recent, list)
    assert recent[0]["model"] == "test-model"


def test_openai_failure_diagnostics_persist_only_allowlisted_fields(session_factory) -> None:
    secret = "sk-never-store-this-provider-body"  # noqa: S105 - redaction sentinel

    class RateLimitFailure(Exception):
        status_code = 429
        request_id = "req_safe_123"
        body: ClassVar[dict[str, object]] = {
            "error": {
                "code": "rate_limit_exceeded",
                "param": "input",
                "message": secret,
            }
        }

    failure = RateLimitFailure(f"provider response included {secret}")
    details = _provider_failure_details(failure)
    with session_factory.begin() as session:
        repository = OpenAIUsageRepository(session)
        row = repository.start_call(
            request_id=None,
            model="test-model",
            prompt_version="diagnostics-v1",
            prompt_hash="1" * 64,
            pricing_snapshot={},
        )
        repository.fail_call(
            row,
            latency_ms=125,
            error_code="openai_rate_limit",
            provider_request_id=details["provider_request_id"],
            exception_class=details["exception_class"],
            http_status=details["http_status"],
            provider_error_code=details["provider_error_code"],
            provider_error_parameter=details["provider_error_parameter"],
            failure_phase="responses_create",
            retryable=details["retryable"],
        )
        call_id = row.id

    with session_factory() as session:
        row = session.get(OpenAICall, call_id)
        assert row is not None
        assert row.application_call_id == call_id
        assert row.provider_request_id == "req_safe_123"
        assert row.exception_class == "RateLimitFailure"
        assert row.http_status == 429
        assert row.provider_error_code == "rate_limit_exceeded"
        assert row.provider_error_parameter == "input"
        assert row.failure_phase == "responses_create"
        assert row.retryable is True
        persisted = "\n".join(
            str(value)
            for value in (
                row.error_code,
                row.provider_request_id,
                row.exception_class,
                row.provider_error_code,
                row.provider_error_parameter,
                row.failure_phase,
            )
        )
        assert secret not in persisted

    snapshot = usage_snapshot(session_factory)
    recent = snapshot["recent"][0]
    assert recent["error_category"] == "Rate limited"
    assert recent["provider_request_id"] == "req_safe_123"
    assert recent["failure_phase"] == "responses_create"
    assert secret not in repr(recent)


def test_post_response_failure_preserves_provider_request_and_usage(session_factory) -> None:
    with session_factory.begin() as session:
        repository = OpenAIUsageRepository(session)
        row = repository.start_call(
            request_id=None,
            model="test-model",
            prompt_version="diagnostics-v1",
            prompt_hash="2" * 64,
            pricing_snapshot={},
        )
        repository.complete_call(
            row,
            response_id="resp_safe_123",
            provider_request_id="req_safe_456",
            usage=UsageValues(input_tokens=10, output_tokens=5, total_tokens=15),
            latency_ms=20,
            service_tier="default",
            estimated_cost_microusd=7,
        )
        repository.fail_call(
            row,
            latency_ms=20,
            error_code="openai_malformed_response",
            failure_phase="structured_output",
            retryable=False,
        )
        call_id = row.id

    with session_factory() as session:
        row = session.get(OpenAICall, call_id)
        assert row is not None
        assert row.status == "failed"
        assert row.provider_request_id == "req_safe_456"
        assert row.response_id == "resp_safe_123"
        assert row.total_tokens == 15
        assert row.estimated_cost_microusd == 7

    daily = usage_snapshot(session_factory)["daily"]
    assert isinstance(daily, list)
    assert daily[0]["estimated_cost_microusd"] == 7


def test_provider_identifiers_are_bounded_and_sanitized(session_factory) -> None:
    unsafe_request_id = "req-safe/\r\nsecret=" + "x" * 200
    with session_factory.begin() as session:
        repository = OpenAIUsageRepository(session)
        row = repository.start_call(
            request_id=None,
            model="test-model",
            prompt_version="diagnostics-v1",
            prompt_hash="3" * 64,
            pricing_snapshot={},
        )
        repository.fail_call(
            row,
            latency_ms=1,
            error_code="openai_error",
            provider_request_id=unsafe_request_id,
        )
        call_id = row.id

    with session_factory() as session:
        row = session.get(OpenAICall, call_id)
        assert row is not None
        assert row.provider_request_id is not None
        assert len(row.provider_request_id) <= 100
        assert "\r" not in row.provider_request_id
        assert "\n" not in row.provider_request_id
        assert "/" not in row.provider_request_id
