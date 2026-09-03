from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.openai_schema import compile_openai_schema
from app.repositories.usage import UsageValues
from app.schemas import MusicProposal


class OpenAINotConfigured(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str


class OpenAIResponsesClient:
    """Thin Responses API adapter with privacy-safe defaults.

    No SDK client is constructed until a key exists, so an installation can
    start and serve health/configuration routes without OpenAI credentials.
    """

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.model = settings.openai_model
        self.reasoning_effort = settings.openai_reasoning_effort
        self.max_tool_calls = settings.openai_max_tool_calls
        self.max_output_tokens = settings.openai_max_output_tokens
        self._owns_client = client is None
        self._client = client
        if self._client is None and settings.openai_api_key is not None:
            key = settings.openai_api_key.get_secret_value().strip()
            if key:
                self._client = AsyncOpenAI(api_key=key, max_retries=2, timeout=60.0)

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if not self._owns_client or self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        self._client = None

    async def create_response(
        self,
        *,
        input_items: str | Sequence[Mapping[str, Any]],
        instructions: str,
        tools: Sequence[Mapping[str, Any]],
        enable_web_search: bool = False,
        web_search_context: str = "low",
        safety_identifier: str | None = None,
        prompt_cache_key: str | None = None,
        text_format: Mapping[str, Any] | None = None,
        max_tool_calls: int | None = None,
        max_output_tokens: int | None = None,
    ) -> Any:
        if self._client is None:
            raise OpenAINotConfigured("OpenAI API key is not configured")
        request_tools = [dict(tool) for tool in tools]
        include = ["reasoning.encrypted_content"]
        if enable_web_search:
            request_tools.append(
                {
                    "type": "web_search_preview",
                    "search_context_size": web_search_context,
                }
            )
            include.append("web_search_call.action.sources")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": input_items,
            "instructions": instructions,
            "tools": request_tools,
            "tool_choice": "auto" if request_tools else "none",
            "parallel_tool_calls": False,
            "store": False,
            "include": include,
            "text": {"format": dict(text_format or music_proposal_format())},
            "max_output_tokens": max_output_tokens or self.max_output_tokens,
            "max_tool_calls": (self.max_tool_calls if max_tool_calls is None else max_tool_calls),
        }
        if safety_identifier:
            kwargs["safety_identifier"] = safety_identifier[:64]
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key[:64]
        if self.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        return await self._client.responses.create(**kwargs)


def music_proposal_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "music_proposal",
        "description": "A bounded, evidence-backed music proposal.",
        "strict": True,
        "schema": strict_json_schema(MusicProposal.model_json_schema()),
    }


def source_selection_format(candidate_ids: Sequence[str]) -> dict[str, Any]:
    identifiers = list(dict.fromkeys(candidate_ids))
    if not identifiers or len(identifiers) > 8:
        raise ValueError("source selection requires 1-8 unique candidate IDs")
    return {
        "type": "json_schema",
        "name": "source_selection",
        "description": "Select only from the finite supplied source IDs, or request review.",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "selected_source_id": {"type": ["string", "null"], "enum": [*identifiers, None]},
                "needs_review": {"type": "boolean"},
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": ["selected_source_id", "needs_review", "rationale"],
            "additionalProperties": False,
        },
    }


def strict_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Compile the exact strict subset accepted by the Responses API."""

    return compile_openai_schema(schema)


def response_output_items(response: Any) -> list[dict[str, Any]]:
    output = _value(response, "output", [])
    if not isinstance(output, Sequence) or isinstance(output, (str, bytes, bytearray)):
        return []
    items: list[dict[str, Any]] = []
    for item in output:
        if isinstance(item, Mapping):
            items.append(dict(item))
            continue
        dump = getattr(item, "model_dump", None)
        if callable(dump):
            value = dump(exclude_none=True)
            if isinstance(value, dict):
                items.append(value)
    return items


def response_function_calls(response: Any) -> list[FunctionCall]:
    calls: list[FunctionCall] = []
    for item in response_output_items(response):
        if item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or "")
        name = str(item.get("name") or "")
        arguments = str(item.get("arguments") or "{}")
        if call_id and name:
            calls.append(FunctionCall(call_id=call_id, name=name, arguments=arguments))
    return calls


def response_output_text(response: Any) -> str:
    direct = _value(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    texts: list[str] = []
    for item in response_output_items(response):
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "output_text":
                value = part.get("text")
                if isinstance(value, str):
                    texts.append(value)
    return "\n".join(texts).strip()


def response_usage(response: Any, *, web_search_context: str | None = None) -> UsageValues:
    usage = _value(response, "usage", {}) or {}
    input_details = _value(usage, "input_tokens_details", {}) or {}
    output_details = _value(usage, "output_tokens_details", {}) or {}
    input_tokens = _integer(_value(usage, "input_tokens", 0))
    output_tokens = _integer(_value(usage, "output_tokens", 0))
    return UsageValues(
        reported=all(
            isinstance(_value(usage, name, None), int)
            and not isinstance(_value(usage, name, None), bool)
            and _value(usage, name, -1) >= 0
            for name in ("input_tokens", "output_tokens")
        ),
        input_tokens=input_tokens,
        cached_input_tokens=_integer(_value(input_details, "cached_tokens", 0)),
        cache_write_tokens=_integer(_value(input_details, "cache_write_tokens", 0)),
        output_tokens=output_tokens,
        reasoning_tokens=_integer(_value(output_details, "reasoning_tokens", 0)),
        total_tokens=_integer(_value(usage, "total_tokens", input_tokens + output_tokens)),
        web_search_count=sum(
            1 for item in response_output_items(response) if item.get("type") == "web_search_call"
        ),
        web_search_context=web_search_context,
    )


def response_id(response: Any) -> str | None:
    value = _value(response, "id", None)
    return str(value) if value else None


def response_request_id(response: Any) -> str | None:
    value = getattr(response, "_request_id", None) or _value(response, "request_id", None)
    return str(value) if value else None


def response_service_tier(response: Any) -> str | None:
    value = _value(response, "service_tier", None)
    return str(value) if value else None


def response_refusal(response: Any) -> str | None:
    direct = _value(response, "refusal", None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()[:500]
    for item in response_output_items(response):
        if item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "refusal":
                value = part.get("refusal") or part.get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()[:500]
    return None


def response_incomplete_reason(response: Any) -> str | None:
    if _value(response, "status", None) != "incomplete":
        return None
    details = _value(response, "incomplete_details", {}) or {}
    reason = _value(details, "reason", None)
    return str(reason or "unknown")[:100]


def _value(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
