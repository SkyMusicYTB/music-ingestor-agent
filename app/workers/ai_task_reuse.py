from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ServiceTask
from app.repositories.decisions import stable_payload

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_REUSABLE_STATES = ("queued", "running", "retry_wait", "completed")


def reuse_or_create_decision_task(
    session: Session,
    *,
    target: str,
    kind: str,
    payload_version: int,
    payload: Mapping[str, object],
) -> ServiceTask:
    """Reuse one durable AI decision task for an exact finite candidate set.

    Callers must validate the active job lease in the same transaction before invoking
    this helper. Candidate ordering is intentionally ignored, while the complete
    normalized intent and matcher policy version are part of the reuse boundary.
    """

    job_id = payload.get("job_id")
    category = payload.get("decision_category")
    fingerprint = payload.get("candidate_set_fingerprint")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("decision task payload requires a job ID")
    if not isinstance(category, str) or not category:
        raise ValueError("decision task payload requires a category")
    if not isinstance(fingerprint, str) or _FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("decision task payload requires a finite candidate fingerprint")

    safe_payload = stable_payload(payload)
    if not isinstance(safe_payload, dict):
        raise ValueError("decision task payload must be an object")
    context = stable_payload(
        {
            "schema_version": safe_payload.get("schema_version"),
            "decision_category": category,
            "candidate_set_fingerprint": fingerprint,
            "intent": safe_payload.get("intent"),
            "matcher_prompt_version": safe_payload.get("matcher_prompt_version"),
        }
    )
    context_serialized = json.dumps(
        context,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    safe_payload["decision_context_fingerprint"] = hashlib.sha256(
        context_serialized.encode("utf-8")
    ).hexdigest()
    serialized = json.dumps(
        safe_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    task = session.scalar(
        select(ServiceTask)
        .where(
            ServiceTask.target == target,
            ServiceTask.kind == kind,
            ServiceTask.payload_version == payload_version,
            ServiceTask.state.in_(_REUSABLE_STATES),
            func.json_extract(ServiceTask.payload_json, "$.job_id") == job_id,
            func.json_extract(ServiceTask.payload_json, "$.decision_category") == category,
            func.json_extract(ServiceTask.payload_json, "$.candidate_set_fingerprint")
            == fingerprint,
            func.json_extract(ServiceTask.payload_json, "$.decision_context_fingerprint")
            == safe_payload["decision_context_fingerprint"],
        )
        .order_by(ServiceTask.created_at.desc())
        .limit(1)
    )
    if task is not None:
        return task
    task = ServiceTask(
        target=target,
        kind=kind,
        payload_version=payload_version,
        payload_json=serialized,
        available_at=datetime.now(UTC),
    )
    session.add(task)
    session.flush()
    return task


__all__ = ["reuse_or_create_decision_task"]
