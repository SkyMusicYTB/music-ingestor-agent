from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import OpenAICall, OpenAIToolCall


@dataclass(frozen=True, slots=True)
class UsageValues:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    web_search_count: int = 0
    web_search_context: str | None = None


class OpenAIUsageRepository:
    """Persists provider usage without ever storing prompt or tool-result bodies."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start_call(
        self,
        *,
        request_id: str | None,
        model: str,
        prompt_version: str,
        prompt_hash: str,
        pricing_snapshot: Mapping[str, Any],
    ) -> OpenAICall:
        row = OpenAICall(
            request_id=request_id,
            model=model,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            status="started",
            pricing_snapshot_json=_json(pricing_snapshot),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def complete_call(
        self,
        row: OpenAICall,
        *,
        response_id: str | None,
        provider_request_id: str | None,
        usage: UsageValues,
        latency_ms: int,
        service_tier: str | None,
        estimated_cost_microusd: int | None,
    ) -> None:
        row.response_id = response_id
        row.provider_request_id = provider_request_id
        row.input_tokens = usage.input_tokens
        row.cached_input_tokens = usage.cached_input_tokens
        row.cache_write_tokens = usage.cache_write_tokens
        row.output_tokens = usage.output_tokens
        row.reasoning_tokens = usage.reasoning_tokens
        row.total_tokens = usage.total_tokens
        row.web_search_count = usage.web_search_count
        row.web_search_context = usage.web_search_context
        row.latency_ms = max(0, latency_ms)
        row.service_tier = service_tier
        row.estimated_cost_microusd = estimated_cost_microusd
        row.status = "completed"
        row.error_code = None

    def fail_call(
        self,
        row: OpenAICall,
        *,
        latency_ms: int,
        error_code: str,
        provider_request_id: str | None = None,
    ) -> None:
        row.latency_ms = max(0, latency_ms)
        row.provider_request_id = provider_request_id
        row.status = "failed"
        row.error_code = error_code[:100]

    def record_tool_call(
        self,
        *,
        openai_call_id: str,
        provider_call_id: str,
        tool_name: str,
        tool_kind: str,
        arguments: Mapping[str, Any],
        result_summary: Mapping[str, Any] | None,
        duration_ms: int,
        status: str,
    ) -> OpenAIToolCall:
        row = OpenAIToolCall(
            openai_call_id=openai_call_id,
            provider_call_id=provider_call_id,
            tool_name=tool_name[:100],
            tool_kind=tool_kind[:20],
            arguments_json=_json(arguments),
            result_summary_json=_json(result_summary) if result_summary is not None else None,
            duration_ms=max(0, duration_ms),
            status=status[:32],
        )
        self._session.add(row)
        self._session.flush()
        return row


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
