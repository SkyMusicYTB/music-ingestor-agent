from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import ServiceTask
from app.repositories.decisions import candidate_set_fingerprint
from app.workers.ai_task_reuse import reuse_or_create_decision_task


def _payload(*, job_id: str, candidate_id: str) -> dict[str, object]:
    candidates = [
        {"candidate_id": candidate_id, "local_score": 0.8},
        {"candidate_id": "stable-candidate", "local_score": 0.7},
    ]
    return {
        "schema_version": 2,
        "job_id": job_id,
        "decision_category": "canonical_metadata",
        "candidate_set_fingerprint": candidate_set_fingerprint("canonical_metadata", candidates),
        "candidates": candidates,
    }


def test_completed_and_pending_decision_tasks_are_reused_by_fingerprint(session_factory) -> None:
    first_payload = _payload(job_id="job-1", candidate_id="candidate-a")
    with session_factory.begin() as session:
        pending = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=first_payload,
        )
        pending_id = pending.id
    with session_factory.begin() as session:
        reordered_payload = dict(first_payload)
        candidates = first_payload["candidates"]
        assert isinstance(candidates, list)
        reordered_payload["candidates"] = list(reversed(candidates))
        reused_pending = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=reordered_payload,
        )
        assert reused_pending.id == pending_id
        reused_pending.state = "completed"
        reused_pending.result_json = '{"decision":{"decision":"match"}}'
    with session_factory.begin() as session:
        reused_completed = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=first_payload,
        )
        assert reused_completed.id == pending_id

        changed = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=_payload(job_id="job-1", candidate_id="candidate-b"),
        )
        assert changed.id != pending_id

    with session_factory() as session:
        assert session.scalar(select(func.count(ServiceTask.id))) == 2


def test_failed_decision_task_is_not_reused(session_factory) -> None:
    payload = _payload(job_id="job-2", candidate_id="candidate-a")
    with session_factory.begin() as session:
        failed = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=payload,
        )
        failed.state = "failed"
        failed_id = failed.id
    with session_factory.begin() as session:
        replacement = reuse_or_create_decision_task(
            session,
            target="web",
            kind="match_canonical",
            payload_version=2,
            payload=payload,
        )
        assert replacement.id != failed_id
