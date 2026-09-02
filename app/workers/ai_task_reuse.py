from __future__ import annotations

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
    this helper. The job/category/fingerprint fields make the idempotency boundary
    explicit instead of depending on prompt prose or candidate display ordering.
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
