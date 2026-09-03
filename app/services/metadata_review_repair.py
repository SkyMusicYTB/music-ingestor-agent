"""Narrow, auditable recovery for the obsolete empty MusicBrainz review."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import immediate_session
from app.db.models import (
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    SourceCandidate,
)
from app.repositories.decisions import candidate_set_fingerprint
from app.repositories.events import make_event
from app.sources import EXECUTABLE_EVIDENCE_KINDS
from app.sources.identities import ProviderIdentity
from app.sources.policy import ProviderURLPolicy
from app.sources.providers import provider_for_extractor

LEGACY_METADATA_ERROR = "MusicBrainz returned no recording candidates"


def obsolete_empty_metadata_review(
    session: Session, job: DownloadJob
) -> tuple[list[JobDecision], SourceCandidate] | None:
    """Recognize only inactive jobs with the confirmed incident's complete shape.

    This is not a source validator: the worker revalidates the retained candidate
    before execution. Recovery never probes URLs, invokes providers, or changes
    source policy, selected metadata, files, or unrelated pending choices.
    """
    if (
        job.status != "needs_review"
        or job.error_message != LEGACY_METADATA_ERROR
        or job.lease_token is not None
        or job.cancel_requested_at is not None
    ):
        return None
    pending = list(
        session.scalars(
            select(JobDecision).where(JobDecision.job_id == job.id, JobDecision.state == "pending")
        )
    )
    empty_fingerprint = candidate_set_fingerprint("acquisition_source", [])
    if not pending or any(
        decision.category != "acquisition_source"
        or decision.candidate_set_fingerprint != empty_fingerprint
        or decision.selected_payload_json is not None
        for decision in pending
    ):
        return None
    if (
        session.scalar(
            select(JobReviewOption.id)
            .where(JobReviewOption.decision_id.in_([decision.id for decision in pending]))
            .limit(1)
        )
        is not None
    ):
        return None
    candidate_id = job.active_source_candidate_id
    if candidate_id is None:
        # Some legacy runs kept only the immutable selected source decision.
        selected = session.scalars(
            select(JobDecision)
            .where(
                JobDecision.job_id == job.id,
                JobDecision.category == "acquisition_source",
                JobDecision.state == "selected",
            )
            .order_by(JobDecision.revision.desc())
            .limit(1)
        ).first()
        try:
            payload = json.loads(selected.selected_payload_json or "null") if selected else None
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("source_candidate_id"), str):
            candidate_id = payload["source_candidate_id"]
    if candidate_id is None:
        return None
    source = session.scalar(
        select(SourceCandidate)
        .outerjoin(EvidenceReference, SourceCandidate.evidence_id == EvidenceReference.id)
        .where(
            SourceCandidate.id == candidate_id,
            SourceCandidate.job_id == job.id,
            SourceCandidate.policy_status == "allowed",
            SourceCandidate.probe_status == "valid",
            SourceCandidate.superseded_by_id.is_(None),
            SourceCandidate.failure_code.is_(None),
            or_(
                SourceCandidate.evidence_id.is_(None),
                EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
            ),
        )
    )
    if (
        source is None
        or source.provider not in {"youtube", "soundcloud", "bandcamp"}
        or not source.acquisition_url
    ):
        return None
    try:
        provider = ProviderIdentity(source.provider)
        contradictions = json.loads(source.contradictions_json)
    except (ValueError, json.JSONDecodeError):
        return None
    if (
        contradictions != []
        or provider_for_extractor(source.extractor) != provider
        or not ProviderURLPolicy().validate(source.acquisition_url, provider=provider).allowed
    ):
        return None
    return pending, source


def repair_empty_metadata_review(session: Session, job: DownloadJob) -> bool:
    """Apply within the caller's short BEGIN IMMEDIATE ownership transaction."""
    match = obsolete_empty_metadata_review(session, job)
    if match is None:
        return False
    decisions, source = match
    now = datetime.now(UTC)
    for decision in decisions:
        decision.state = "superseded"
        decision.decided_at = now
    job.active_source_candidate_id = source.id
    job.status = "queued"
    job.stage = "resolving_metadata"
    job.available_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.error_code = None
    job.error_message = None
    job.updated_at = now
    try:
        warnings = json.loads(job.warnings_json or "[]")
    except json.JSONDecodeError:
        warnings = []
    if not isinstance(warnings, list):
        warnings = []
    job.warnings_json = json.dumps(
        [
            warning
            for warning in warnings
            if isinstance(warning, dict)
            and warning.get("code") != "needs_review"
            and warning.get("message") != LEGACY_METADATA_ERROR
        ][-20:],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    session.add(
        make_event(
            session,
            entity_type="job",
            entity_id=job.id,
            event_type="job.metadata_review_repaired",
            message="Obsolete empty metadata review repaired; the selected source was retained",
            details={"superseded_decisions": len(decisions)},
        )
    )
    return True


def repair_empty_metadata_reviews(
    factory: sessionmaker[Session],
    *,
    apply: bool = False,
    source_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, object]:
    """Inspect read-only by default; applying rechecks each inactive job atomically."""
    repaired: list[str] = []
    statement = (
        select(DownloadJob.id)
        .where(
            DownloadJob.status == "needs_review",
            DownloadJob.error_message == LEGACY_METADATA_ERROR,
            DownloadJob.lease_token.is_(None),
        )
        .order_by(DownloadJob.id)
    )
    if job_id:
        statement = statement.where(DownloadJob.id == job_id)
    with factory() as session:
        job_ids = list(session.scalars(statement))
    for candidate_job_id in job_ids:
        # One short transaction per job: a large cleanup must not hold the
        # worker/web database lock while inspecting all other jobs.
        with immediate_session(factory) if apply else factory() as session:
            job = session.get(DownloadJob, candidate_job_id)
            if job is not None:
                match = obsolete_empty_metadata_review(session, job)
                if match is None or (source_id is not None and match[1].source_id != source_id):
                    continue
                if not apply or repair_empty_metadata_review(session, job):
                    repaired.append(job.id)
    return {"mode": "apply" if apply else "dry-run", "count": len(repaired), "job_ids": repaired}
