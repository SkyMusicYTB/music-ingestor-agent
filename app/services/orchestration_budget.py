"""Bounded, in-memory discovery state; never persist model reasoning or transcripts."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.schemas import MusicProposal
from app.services.proposals import VerifiedMetadata
from app.tools.registry import ToolExecution


@dataclass
class DiscoveryAttempt:
    request_id: str
    attempt_id: str
    lease_token: str
    deadline: float
    rounds_used: int = 0
    termination_reason: str | None = None
    partial: MusicProposal | None = None
    verified: dict[str, VerifiedMetadata] = field(default_factory=dict)
    lease_lost: bool = False

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


current_attempt: ContextVar[DiscoveryAttempt | None] = ContextVar(
    "music_discovery_attempt", default=None
)


def _normalized(value: Any, *, result: bool = False, field_name: str = "") -> Any:
    if isinstance(value, str):
        if field_name in {"id", "ids", "url", "uri"} or field_name.endswith(
            ("_id", "_ids", "_mbid", "_mbids", "_url")
        ):
            # Executable provider identifiers (notably YouTube IDs) are case
            # sensitive. Text-query equivalence must not collapse identities.
            return value
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if isinstance(value, dict):
        ignored = {"cached", "duration_ms", "view_count", "accessed_at", "fetched_at"}
        return {
            key: _normalized(item, result=result, field_name=key)
            for key, item in sorted(value.items())
            if not result or key not in ignored
        }
    if isinstance(value, list):
        items = [_normalized(item, result=result, field_name=field_name) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) if result else items
    return value


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class NoProgressDetector:
    """Three unchanged answers in one scope force synthesis, not failure.

    Separate tracks/queries retain independent scopes. Broader limits count as
    progress only if the result actually expands. Volatile cache markers and
    result ordering do not disguise a repeated result.
    """

    threshold: int = 3
    last_scope: str | None = None
    last_result: str | None = None
    last_round: int | None = None
    consecutive_repeats: int = 0
    observations: int = 0

    def observe(self, execution: ToolExecution, *, round_number: int | None = None) -> bool:
        self.observations += 1
        round_number = self.observations if round_number is None else round_number
        arguments = _normalized(execution.arguments)
        # A larger result limit with identical content is not new evidence.
        scope = _fingerprint(
            {
                "tool": execution.name,
                "arguments": {k: v for k, v in arguments.items() if k != "limit"},
            }
        )
        try:
            envelope = json.loads(execution.output)
        except (ValueError, TypeError):
            envelope = {"status": execution.status}
        result = _fingerprint(
            {"tool": execution.name, "result": _normalized(envelope, result=True)}
        )
        # Equivalent nonempty candidate sets remain equivalent if the model
        # merely rephrases its query. Empty results stay query-scoped so three
        # different unavailable tracks do not stop a legitimate bulk request.
        nonempty = _has_candidates(envelope.get("result")) if isinstance(envelope, dict) else False
        equivalent = self.last_result == result and (self.last_scope == scope or nonempty)
        if not equivalent:
            # Interleaved legitimate discovery and enriched results reset the
            # streak. No lifetime counter can stop A, B, A, B, A discovery.
            self.consecutive_repeats = 1
        elif self.last_round != round_number:
            self.consecutive_repeats += 1
        self.last_scope, self.last_result, self.last_round = scope, result, round_number
        return self.consecutive_repeats >= self.threshold


def _has_candidates(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return any(_has_candidates(item) for item in value.values())
    return False
