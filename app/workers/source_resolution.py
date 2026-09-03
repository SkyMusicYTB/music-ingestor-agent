from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
    validate_public_media_metadata,
)
from app.config import Settings
from app.db.models import (
    DownloadJob,
    EvidenceReference,
    JobDecision,
    RequestTrack,
    ServiceTask,
)
from app.db.models import (
    SourceCandidate as DbSourceCandidate,
)
from app.repositories.decisions import candidate_set_fingerprint, record_selected_decision
from app.services.artist_credits import structured_artists
from app.sources import (
    EXECUTABLE_EVIDENCE_KINDS,
    FiniteSourceResolver,
    MatchDecision,
    ProviderIdentity,
    SourceCandidate,
    SourceIntent,
    SourcePolicy,
    UploaderRelationship,
    adjudicate_ai_source_match,
    bound_provider_description,
    decide_source_match,
    group_ranked_sources,
    provider_capability,
    provider_for_url,
    rank_sources,
    resolve_provider_recording_metadata,
)
from app.workers.ai_task_reuse import reuse_or_create_decision_task
from app.workers.queue import DownloadJobQueue, JobLease, LeaseLostError
from app.workers.source_failures import is_transient_source_error


class LeaseMonitor(Protocol):
    def raise_if_unusable(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    candidate_id: str
    url: str
    provider: str
    extractor: str
    source_id: str


class SourceResolutionNeedsReview(RuntimeError):
    def __init__(self, reason: str, options: list[dict[str, object]]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.options = options


class WorkerSourceResolver:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        queue: DownloadJobQueue,
        ytdlp: YtDlpClient,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.queue = queue
        self.ytdlp = ytdlp
        self.policy = SourcePolicy(
            max_candidates=settings.max_source_candidates,
            visible_candidates=settings.max_visible_source_options,
            max_attempts=settings.max_automatic_source_attempts,
            auto_threshold=settings.source_auto_select_threshold,
            minimum_lead=settings.source_ambiguity_margin,
            ai_confidence_threshold=settings.ai_match_auto_accept_threshold,
            ai_local_score_threshold=settings.ai_match_min_local_score,
            allowed_providers=tuple(
                ProviderIdentity(value) for value in settings.enabled_media_providers
            ),
            provider_preference=tuple(
                ProviderIdentity(value) for value in settings.media_provider_preference
            ),
        )

    def resolve(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
    ) -> ResolvedSource:
        active = self._active_candidate(lease)
        if active is not None:
            return active
        intent = SourceIntent(
            artist=_required_string(lease.approved_snapshot, "artist"),
            title=_required_string(lease.approved_snapshot, "title"),
            requested_version=_requested_source_version(
                lease.approved_snapshot,
                self.settings.default_version_preference,
            ),
            duration_seconds=_optional_float(lease.approved_snapshot.get("duration_seconds")),
        )
        provider_scope = _effective_provider_scope(lease.approved_snapshot)
        candidates = self._discover(
            lease,
            intent,
            cancellation,
            provider_scope=provider_scope,
        )
        if not candidates:
            fallback_review = self.provider_fallback_review(lease)
            if fallback_review is not None:
                reason, options = fallback_review
                raise SourceResolutionNeedsReview(reason, options)
            raise SourceResolutionNeedsReview("no permitted media source was found", [])
        resolver = FiniteSourceResolver(candidates, max_candidates=self.policy.max_candidates)
        ranked = rank_sources(intent, candidates, policy=self.policy)
        self._persist_rankings(lease, ranked)
        decision = decide_source_match(intent, candidates, policy=self.policy, resolver=resolver)
        authority = "deterministic"
        call_id: str | None = None
        if decision.decision is MatchDecision.AMBIGUOUS:
            if self.settings.ai_match_resolution_enabled:
                model_result = self._ask_openai(
                    lease,
                    monitor,
                    cancellation,
                    intent,
                    resolver,
                    ranked,
                )
                call_id = _optional_string(model_result.get("openai_call_id"))
                raw_decision = model_result.get("decision")
                try:
                    decision = adjudicate_ai_source_match(
                        intent,
                        raw_decision if isinstance(raw_decision, Mapping) else {},
                        candidates,
                        policy=self.policy,
                        resolver=resolver,
                    )
                except (TypeError, ValueError):
                    decision = decide_source_match(
                        intent, candidates, policy=self.policy, resolver=resolver
                    )
                authority = "openai"
        if decision.decision is not MatchDecision.MATCH:
            raise SourceResolutionNeedsReview(
                _review_reason(decision.reason_code),
                self._review_options(lease.job_id, ranked, resolver),
            )
        selected_id = decision.selected_source_candidate_id
        if selected_id is None:
            raise SourceResolutionNeedsReview("source resolution returned no candidate", [])
        selected = resolver.resolve(selected_id)
        row = self._select_candidate(
            lease,
            selected,
            selected_id,
            resolver,
            ranked,
            authority=authority,
            local_confidence=next(
                item.score for item in ranked if item.candidate.identity == selected.identity
            ),
            model_confidence=(decision.confidence if authority == "openai" else None),
            openai_call_id=call_id,
            reason_code=decision.reason_code,
        )
        return _resolved(row)

    def provider_fallback_review(
        self, lease: JobLease
    ) -> tuple[str, list[dict[str, object]]] | None:
        requested_providers = _requested_provider_scope(lease.approved_snapshot)
        excluded_providers = _excluded_provider_scope(lease.approved_snapshot)
        if (not requested_providers and not excluded_providers) or _provider_fallback_allowed(
            lease.approved_snapshot
        ):
            return None
        current_scope = _effective_provider_scope(lease.approved_snapshot) or frozenset()
        alternatives = [
            provider.value
            for provider in self.policy.provider_preference
            if provider in self.policy.allowed_providers and provider not in current_scope
        ]
        if not alternatives:
            return None
        requested_provider = (
            next(iter(requested_providers))
            if len(requested_providers) == 1 and not excluded_providers
            else None
        )
        if requested_provider is not None:
            reason = (
                f"The explicitly requested {requested_provider.value} source is unavailable. "
                "Permission is needed before another provider can be used."
            )
        else:
            reason = (
                "No source matched the providers allowed by this request. "
                "Permission is needed before using a provider outside that constraint."
            )
        option: dict[str, object] = {
            "kind": "acquisition_source",
            "rank": 1,
            "allow_provider_fallback": True,
            "requested_providers": sorted(provider.value for provider in requested_providers),
            "excluded_providers": sorted(provider.value for provider in excluded_providers),
            "fallback_providers": alternatives,
            "provider": "automatic",
            "title": "Use another permitted provider",
            "score": 1.0,
            "materially_different": True,
        }
        if requested_provider is not None:
            option["requested_provider"] = requested_provider.value
        return (
            reason,
            [option],
        )

    def reject_active(self, lease: JobLease, error_code: str) -> int:
        with self.factory.begin() as session:
            job = _leased_job(session, lease, action="rejecting a source candidate")
            if job.active_source_candidate_id:
                candidate = session.get(DbSourceCandidate, job.active_source_candidate_id)
                if candidate is not None:
                    candidate.failure_code = error_code[:100]
                    candidate.policy_status = "exhausted"
                    candidate.attempted_at = datetime.now(UTC)
                decision = session.scalar(
                    select(JobDecision)
                    .where(
                        JobDecision.job_id == job.id,
                        JobDecision.category == "acquisition_source",
                        JobDecision.state == "selected",
                    )
                    .order_by(JobDecision.revision.desc())
                    .limit(1)
                )
                if decision is not None:
                    decision.state = "rejected"
                    reasons = _json_string_list(decision.reason_codes_json)
                    reasons.append(f"source_failed:{error_code[:80]}")
                    decision.reason_codes_json = json.dumps(
                        list(dict.fromkeys(reasons)), separators=(",", ":")
                    )
            job.active_source_candidate_id = None
            job.source_attempt_count += 1
            return job.source_attempt_count

    def _active_candidate(self, lease: JobLease) -> ResolvedSource | None:
        with self.factory.begin() as session:
            job = _leased_job(session, lease, action="restoring a source candidate")
            if not job.active_source_candidate_id:
                return None
            row = session.scalar(
                select(DbSourceCandidate)
                .outerjoin(
                    EvidenceReference,
                    DbSourceCandidate.evidence_id == EvidenceReference.id,
                )
                .where(
                    DbSourceCandidate.id == job.active_source_candidate_id,
                    DbSourceCandidate.job_id == lease.job_id,
                    or_(
                        DbSourceCandidate.evidence_id.is_(None),
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                    ),
                    DbSourceCandidate.policy_status == "allowed",
                    DbSourceCandidate.probe_status == "valid",
                    DbSourceCandidate.acquisition_url.is_not(None),
                )
            )
            if row is None:
                job.active_source_candidate_id = None
                return None
            provider_scope = _effective_provider_scope(lease.approved_snapshot)
            if provider_scope is not None and row.provider not in {
                provider.value for provider in provider_scope
            }:
                # Queue-time candidate attachment is deliberately conservative and
                # may predate the worker's authoritative provider constraint check.
                job.active_source_candidate_id = None
                return None
            existing_decision = session.scalar(
                select(JobDecision.id).where(
                    JobDecision.job_id == job.id,
                    JobDecision.category == "acquisition_source",
                    JobDecision.state == "selected",
                )
            )
            if existing_decision is None:
                record_selected_decision(
                    session,
                    job,
                    category="acquisition_source",
                    candidates=[_decision_candidate(row)],
                    selected_payload={
                        "source_candidate_id": row.id,
                        "provider": row.provider,
                        "extractor": row.extractor,
                        "source_id": row.source_id,
                    },
                    decided_by="deterministic",
                    reason_codes=["prevalidated_direct_source"],
                    local_confidence=row.local_score,
                )
            return _resolved(row)

    def _discover(
        self,
        lease: JobLease,
        intent: SourceIntent,
        cancellation: CancellationSignal,
        *,
        provider_scope: frozenset[ProviderIdentity] | None,
    ) -> tuple[SourceCandidate, ...]:
        request_track_id = _required_string(lease.approved_snapshot, "request_track_id")
        request_id, request_track_count = self._request_context(request_track_id)
        if request_track_count == 1:
            self._adopt_request_evidence(request_id, request_track_id, lease)
        exhausted = self._exhausted_identity_keys(request_track_id)
        existing = tuple(
            candidate
            for candidate in self._existing_domain_candidates(request_id, request_track_id, lease)
            if provider_scope is None or candidate.provider in provider_scope
        )
        needed = max(0, self.policy.max_candidates - len(existing))
        discovered: list[SourceCandidate] = list(existing)
        if needed:
            discovered.extend(
                self._evidence_candidates(
                    request_id,
                    request_track_id,
                    needed,
                    cancellation,
                    excluded={item.identity.stable_key for item in discovered} | exhausted,
                    provider_scope=provider_scope,
                )
            )
        query = f"{intent.artist} {intent.title}"
        for provider in self.policy.provider_preference:
            if len(discovered) >= self.policy.max_candidates:
                break
            if provider not in self.policy.allowed_providers:
                continue
            if provider_scope is not None and provider not in provider_scope:
                continue
            if provider is ProviderIdentity.BANDCAMP:
                continue
            limit = min(8, self.policy.max_candidates - len(discovered))
            try:
                payload = self.ytdlp.search_provider(
                    query,
                    provider=provider,
                    limit=limit,
                    cancel_signal=cancellation,
                )
            except (SourceValidationError, YtDlpError) as exc:
                if is_transient_source_error(exc):
                    raise
                continue
            entries = payload.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries[:limit]:
                flat = _candidate_from_metadata(provider, entry)
                if (
                    flat is None
                    or flat.identity.stable_key in exhausted
                    or any(item.identity == flat.identity for item in discovered)
                ):
                    continue
                try:
                    full = self.ytdlp.probe(flat.url, cancel_signal=cancellation)
                    candidate = _candidate_from_metadata(provider, full, fallback=flat)
                except (SourceValidationError, YtDlpError) as exc:
                    if is_transient_source_error(exc):
                        raise
                    continue
                if candidate is not None and candidate.identity.stable_key not in exhausted:
                    discovered.append(candidate)
                if len(discovered) >= self.policy.max_candidates:
                    break
        unique = {candidate.identity.stable_key: candidate for candidate in discovered}
        bounded = tuple(unique[key] for key in sorted(unique)[: self.policy.max_candidates])
        self._persist_candidates(request_track_id, lease, bounded)
        return bounded

    def _evidence_candidates(
        self,
        request_id: str,
        request_track_id: str,
        limit: int,
        cancellation: CancellationSignal,
        *,
        excluded: set[str],
        provider_scope: frozenset[ProviderIdentity] | None,
    ) -> list[SourceCandidate]:
        with self.factory() as session:
            evidence = list(
                session.scalars(
                    select(EvidenceReference)
                    .where(
                        or_(
                            EvidenceReference.request_track_id == request_track_id,
                            and_(
                                EvidenceReference.request_id == request_id,
                                EvidenceReference.request_track_id.is_(None),
                            ),
                        ),
                        EvidenceReference.status == "available",
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                        EvidenceReference.canonical_url.is_not(None),
                    )
                    .order_by(EvidenceReference.created_at)
                    # Read a bounded superset so exhausted or already-persisted
                    # references do not hide the next safe candidate.
                    .limit(
                        min(
                            limit + len(excluded),
                            self.policy.max_candidates + self.policy.max_attempts,
                        )
                    )
                )
            )
        result: list[SourceCandidate] = []
        for reference in evidence:
            provider = provider_for_url(reference.canonical_url or "")
            if provider not in self.policy.allowed_providers:
                continue
            if provider_scope is not None and provider not in provider_scope:
                continue
            if reference.provider_item_id:
                identity_key = (
                    f"{provider.value}:{provider_capability(provider).canonical_extractor}:"
                    f"{reference.provider_item_id}"
                )
                if identity_key in excluded:
                    continue
            try:
                metadata = self.ytdlp.probe(
                    reference.canonical_url or "", cancel_signal=cancellation
                )
            except (SourceValidationError, YtDlpError) as exc:
                if is_transient_source_error(exc):
                    raise
                continue
            candidate = _candidate_from_metadata(provider, metadata)
            if candidate is not None and candidate.identity.stable_key not in excluded:
                result.append(candidate)
                if len(result) >= limit:
                    break
        return result

    def _existing_domain_candidates(
        self, request_id: str, request_track_id: str, lease: JobLease
    ) -> tuple[SourceCandidate, ...]:
        with self.factory.begin() as session:
            rows = list(
                session.scalars(
                    select(DbSourceCandidate)
                    .outerjoin(
                        EvidenceReference,
                        DbSourceCandidate.evidence_id == EvidenceReference.id,
                    )
                    .where(
                        or_(
                            and_(
                                DbSourceCandidate.request_track_id == request_track_id,
                                or_(
                                    DbSourceCandidate.evidence_id.is_(None),
                                    EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                                ),
                            ),
                            and_(
                                DbSourceCandidate.request_track_id.is_(None),
                                EvidenceReference.request_id == request_id,
                                EvidenceReference.request_track_id.is_(None),
                                EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                            ),
                        ),
                        DbSourceCandidate.policy_status == "allowed",
                        DbSourceCandidate.probe_status == "valid",
                        DbSourceCandidate.failure_code.is_(None),
                        DbSourceCandidate.acquisition_url.is_not(None),
                    )
                    .order_by(DbSourceCandidate.created_at)
                )
            )
            _leased_job(session, lease, action="restoring source candidates")
            for row in rows:
                if row.request_track_id == request_track_id:
                    row.job_id = lease.job_id
        candidates: list[SourceCandidate] = []
        for row in rows:
            try:
                candidates.append(_domain_from_row(row))
            except ValueError:
                continue
        return tuple(candidates)

    def _request_context(self, request_track_id: str) -> tuple[str, int]:
        with self.factory() as session:
            request_id = session.scalar(
                select(RequestTrack.request_id).where(RequestTrack.id == request_track_id)
            )
            if request_id is None:
                raise LookupError(request_track_id)
            count = session.scalar(
                select(func.count(RequestTrack.id)).where(RequestTrack.request_id == request_id)
            )
        return request_id, int(count or 0)

    def _adopt_request_evidence(
        self,
        request_id: str,
        request_track_id: str,
        lease: JobLease,
    ) -> None:
        """Bind orchestration-time evidence once an exact request has one durable track."""

        with self.factory.begin() as session:
            _leased_job(session, lease, action="binding request evidence")
            evidence_rows = list(
                session.scalars(
                    select(EvidenceReference).where(
                        EvidenceReference.request_id == request_id,
                        EvidenceReference.request_track_id.is_(None),
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                    )
                )
            )
            evidence_ids = {row.id for row in evidence_rows}
            for evidence in evidence_rows:
                evidence.request_track_id = request_track_id
                evidence.job_id = lease.job_id
            if not evidence_ids:
                return
            candidates = list(
                session.scalars(
                    select(DbSourceCandidate).where(
                        DbSourceCandidate.evidence_id.in_(evidence_ids),
                        DbSourceCandidate.request_track_id.is_(None),
                    )
                )
            )
            for candidate in candidates:
                existing = session.scalar(
                    select(DbSourceCandidate).where(
                        DbSourceCandidate.request_track_id == request_track_id,
                        DbSourceCandidate.provider == candidate.provider,
                        DbSourceCandidate.extractor == candidate.extractor,
                        DbSourceCandidate.source_id == candidate.source_id,
                    )
                )
                if existing is None:
                    candidate.request_track_id = request_track_id
                    candidate.job_id = lease.job_id
                else:
                    candidate.superseded_by_id = existing.id
                    candidate.policy_status = "rejected"

    def _exhausted_identity_keys(self, request_track_id: str) -> set[str]:
        with self.factory() as session:
            rows = session.execute(
                select(
                    DbSourceCandidate.provider,
                    DbSourceCandidate.extractor,
                    DbSourceCandidate.source_id,
                ).where(
                    DbSourceCandidate.request_track_id == request_track_id,
                    DbSourceCandidate.policy_status == "exhausted",
                )
            )
            return {
                f"{provider}:{extractor}:{source_id}" for provider, extractor, source_id in rows
            }

    def _persist_candidates(
        self,
        request_track_id: str,
        lease: JobLease,
        candidates: tuple[SourceCandidate, ...],
    ) -> None:
        with self.factory.begin() as session:
            _leased_job(session, lease, action="persisting source candidates")
            for candidate in candidates:
                row = session.scalar(
                    select(DbSourceCandidate).where(
                        DbSourceCandidate.request_track_id == request_track_id,
                        DbSourceCandidate.provider == candidate.provider.value,
                        DbSourceCandidate.extractor == candidate.extractor,
                        DbSourceCandidate.source_id == candidate.source_id,
                    )
                )
                values = _db_candidate_values(candidate)
                if row is None:
                    row = DbSourceCandidate(
                        request_track_id=request_track_id,
                        job_id=lease.job_id,
                        **values,
                    )
                    session.add(row)
                else:
                    if row.policy_status == "exhausted":
                        continue
                    if row.evidence_id is not None:
                        evidence_kind = session.scalar(
                            select(EvidenceReference.evidence_kind).where(
                                EvidenceReference.id == row.evidence_id
                            )
                        )
                        if evidence_kind not in EXECUTABLE_EVIDENCE_KINDS:
                            # This identity was independently rediscovered and probed
                            # by the worker. Sever any legacy model-evidence provenance
                            # before it can become the executable trust basis.
                            row.evidence_id = None
                    row.job_id = lease.job_id
                    for key, value in values.items():
                        setattr(row, key, value)

    def _select_candidate(
        self,
        lease: JobLease,
        selected: SourceCandidate,
        finite_id: str,
        resolver: FiniteSourceResolver,
        ranked: tuple[Any, ...],
        *,
        authority: str,
        local_confidence: float,
        model_confidence: float | None,
        openai_call_id: str | None,
        reason_code: str,
    ) -> DbSourceCandidate:
        with self.factory.begin() as session:
            job = _leased_job(session, lease, action="selecting a source candidate")
            row = session.scalar(
                select(DbSourceCandidate)
                .outerjoin(
                    EvidenceReference,
                    DbSourceCandidate.evidence_id == EvidenceReference.id,
                )
                .where(
                    DbSourceCandidate.job_id == lease.job_id,
                    or_(
                        DbSourceCandidate.evidence_id.is_(None),
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                    ),
                    DbSourceCandidate.provider == selected.provider.value,
                    DbSourceCandidate.extractor == selected.extractor,
                    DbSourceCandidate.source_id == selected.source_id,
                    DbSourceCandidate.policy_status == "allowed",
                    DbSourceCandidate.probe_status == "valid",
                )
            )
            if row is None or row.acquisition_url is None:
                raise SourceResolutionNeedsReview("selected source is no longer available", [])
            records = [
                {
                    "source_candidate_id": resolver.candidate_id_for(item.candidate),
                    "provider": item.candidate.provider.value,
                    "source_id": item.candidate.source_id,
                    "local_score": round(item.score, 6),
                    "contradictions": list(item.contradiction_codes),
                }
                for item in ranked
            ]
            record_selected_decision(
                session,
                job,
                category="acquisition_source",
                candidates=records,
                selected_payload={
                    "source_candidate_id": row.id,
                    "finite_candidate_id": finite_id,
                    "provider": row.provider,
                    "extractor": row.extractor,
                    "source_id": row.source_id,
                },
                decided_by=authority,
                reason_codes=[reason_code],
                local_confidence=local_confidence,
                model_confidence=model_confidence,
                openai_call_id=openai_call_id,
                prompt_version="source_matcher_v2",
            )
            job.active_source_candidate_id = row.id
            job.source_extractor = row.extractor[:40]
            job.source_id = row.source_id[:100]
            row.attempted_at = datetime.now(UTC)
            session.flush()
            return row

    def _persist_rankings(self, lease: JobLease, ranked: tuple[Any, ...]) -> None:
        with self.factory.begin() as session:
            _leased_job(session, lease, action="persisting source rankings")
            for item in ranked:
                candidate = item.candidate
                row = session.scalar(
                    select(DbSourceCandidate).where(
                        DbSourceCandidate.job_id == lease.job_id,
                        DbSourceCandidate.provider == candidate.provider.value,
                        DbSourceCandidate.extractor == candidate.extractor,
                        DbSourceCandidate.source_id == candidate.source_id,
                    )
                )
                if row is None or row.policy_status == "exhausted":
                    continue
                row.local_score = item.score
                row.contradictions_json = json.dumps(
                    list(item.contradiction_codes), separators=(",", ":")
                )
                metadata = _json_object(row.sanitized_metadata_json)
                metadata["ranking_facts"] = {
                    "canonical_match": item.components.canonical_match,
                    "requested_version": item.components.requested_version,
                    "duration_compatibility": item.components.duration_compatibility,
                    "audio_availability_quality": item.components.audio_availability_quality,
                    "uploader_relationship": item.components.uploader_relationship,
                    "provider_reliability": item.components.provider_reliability,
                    "provider_preference": item.components.provider_preference,
                    "version_match": item.version_match,
                    "duration_compatible": item.duration_compatible,
                    "canonical_exact": item.canonical_exact,
                    "contradiction_codes": list(item.contradiction_codes),
                }
                row.sanitized_metadata_json = json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":")
                )

    def _review_options(
        self,
        job_id: str,
        ranked: tuple[Any, ...],
        resolver: FiniteSourceResolver,
    ) -> list[dict[str, object]]:
        groups = group_ranked_sources(ranked)
        options: list[dict[str, object]] = []
        with self.factory() as session:
            for group in groups:
                item = group.best
                candidate = item.candidate
                row = session.scalar(
                    select(DbSourceCandidate).where(
                        DbSourceCandidate.job_id == job_id,
                        DbSourceCandidate.provider == candidate.provider.value,
                        DbSourceCandidate.extractor == candidate.extractor,
                        DbSourceCandidate.source_id == candidate.source_id,
                    )
                )
                if row is None:
                    continue
                options.append(
                    {
                        "kind": "acquisition_source",
                        "rank": len(options) + 1,
                        "source_candidate_id": row.id,
                        "finite_candidate_id": resolver.candidate_id_for(candidate),
                        "provider": candidate.provider.value,
                        "title": candidate.title,
                        "artist": candidate.artist,
                        "uploader": candidate.uploader_name,
                        "uploader_relationship": candidate.uploader_relationship.value,
                        "duration_seconds": candidate.duration_seconds,
                        "score": item.score,
                        "materially_different": True,
                    }
                )
                if len(options) >= self.settings.max_visible_source_options:
                    break
        return options

    def _ask_openai(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
        intent: SourceIntent,
        resolver: FiniteSourceResolver,
        ranked: tuple[Any, ...],
    ) -> dict[str, object]:
        records = []
        for item in ranked[:8]:
            candidate = item.candidate
            records.append(
                {
                    "source_candidate_id": resolver.candidate_id_for(candidate),
                    "provider": candidate.provider.value,
                    "title": candidate.title,
                    "provider_artist": candidate.artist,
                    "track": candidate.track,
                    "uploader": candidate.uploader_name,
                    "uploader_relationship": candidate.uploader_relationship.value,
                    "duration_seconds": candidate.duration_seconds,
                    "local_score": round(item.score, 6),
                    "version_match": item.version_match,
                    "contradiction_codes": list(item.contradiction_codes),
                    "description_untrusted": candidate.description,
                }
            )
        request_id = None
        request_track_id = _required_string(lease.approved_snapshot, "request_track_id")
        with self.factory() as session:
            request_id = session.scalar(
                select(RequestTrack.request_id).where(RequestTrack.id == request_track_id)
            )
        payload = {
            "schema_version": 2,
            "request_id": request_id,
            "job_id": lease.job_id,
            "decision_category": "acquisition_source",
            "intent": intent.model_dump(mode="json"),
            "candidates": records,
        }
        payload["candidate_set_fingerprint"] = candidate_set_fingerprint(
            "acquisition_source", records
        )
        with self.factory.begin() as session:
            _leased_job(session, lease, action="requesting source selection")
            task = reuse_or_create_decision_task(
                session,
                target="web",
                kind="select_source",
                payload_version=2,
                payload=payload,
            )
            task_id = task.id
        self.queue.set_progress(lease, stage="waiting_ai", progress=0.04)
        deadline = time.monotonic() + float(self.settings.max_agent_seconds + 5)
        while time.monotonic() < deadline:
            if cancellation.is_set():
                raise InterruptedError("source selection was cancelled")
            monitor.raise_if_unusable()
            with self.factory() as session:
                row = session.get(ServiceTask, task_id)
                if row is None:
                    return {"decision": {}}
                if row.state == "completed":
                    try:
                        result = json.loads(row.result_json or "{}")
                    except json.JSONDecodeError:
                        return {"decision": {}}
                    return result if isinstance(result, dict) else {"decision": {}}
                if row.state == "failed":
                    return {"decision": {}}
            time.sleep(0.2)
        return {"decision": {}}


def _candidate_from_metadata(
    provider: ProviderIdentity,
    value: object,
    *,
    fallback: SourceCandidate | None = None,
) -> SourceCandidate | None:
    if not isinstance(value, Mapping):
        return fallback
    try:
        validate_public_media_metadata(value)
    except SourceValidationError:
        return None
    source_id = _optional_string(value.get("id")) or (fallback.source_id if fallback else None)
    title = _optional_string(value.get("title")) or (fallback.title if fallback else None)
    if source_id is None or title is None:
        return fallback
    url = _provider_url(provider, value, source_id) or (fallback.url if fallback else None)
    if url is None:
        return fallback
    recording = resolve_provider_recording_metadata(value, fallback_title=title)
    provider_artist = recording.artist or (fallback.artist if fallback else None)
    track = recording.title or (fallback.track if fallback else None)
    uploader = recording.uploader or (fallback.uploader_name if fallback else None)
    relationship = _uploader_relationship(provider_artist, uploader, value)
    duration = _optional_float(value.get("duration"))
    if duration is not None and duration > 14_400:
        return None
    abr = _optional_float(value.get("abr"))
    quality = min(1.0, max(0.1, (abr or 128.0) / 256.0))
    try:
        return SourceCandidate(
            source_id=source_id[:200],
            provider=provider,
            extractor=provider_capability(provider).canonical_extractor,
            url=url,
            title=title[:500],
            artist=provider_artist[:300] if provider_artist else None,
            artists=recording.artists or (fallback.artists if fallback else ()),
            artist_source=recording.artist_source or (fallback.artist_source if fallback else None),
            track=track[:300] if track else None,
            version=_first_string(value, "version"),
            duration_seconds=duration,
            uploader_name=uploader[:300] if uploader else None,
            uploader_id=_first_string(value, "uploader_id", "channel_id"),
            uploader_relationship=relationship,
            audio_available=_optional_string(value.get("acodec")) != "none",
            audio_quality=quality,
            description=bound_provider_description(value.get("description")),
        )
    except ValueError:
        return fallback


def _requested_source_version(snapshot: Mapping[str, object], default: str) -> str:
    """Use only a user-authored version constraint, otherwise operator policy.

    ``version_signature`` can originate in model proposal text and therefore is
    not authoritative for unattended source selection.
    """

    return _optional_string(snapshot.get("requested_version")) or default


def _provider_url(
    provider: ProviderIdentity, value: Mapping[str, object], source_id: str
) -> str | None:
    if provider is ProviderIdentity.YOUTUBE:
        return f"https://www.youtube.com/watch?v={source_id}"
    for key in ("webpage_url", "original_url", "url"):
        candidate = _optional_string(value.get(key))
        if (
            candidate
            and candidate.startswith("https://")
            and provider_for_url(candidate) is provider
        ):
            return candidate
    return None


def _uploader_relationship(
    provider_artist: str | None,
    uploader: str | None,
    metadata: Mapping[str, object],
) -> UploaderRelationship:
    if uploader is None:
        return UploaderRelationship.UNKNOWN
    normalized = " ".join(uploader.casefold().split())
    artist = " ".join((provider_artist or "").casefold().split())
    if normalized.endswith(" - topic") or normalized.endswith(" topic"):
        return UploaderRelationship.TOPIC
    if artist and (normalized == artist or normalized.startswith(f"{artist} official")):
        return UploaderRelationship.OFFICIAL_ARTIST
    description = (_optional_string(metadata.get("description")) or "").casefold()
    if "provided to" in description or "distributed by" in description:
        return UploaderRelationship.DISTRIBUTOR
    return UploaderRelationship.THIRD_PARTY


def _db_candidate_values(candidate: SourceCandidate) -> dict[str, object]:
    return {
        "provider": candidate.provider.value,
        "extractor": candidate.extractor,
        "source_id": candidate.source_id,
        "acquisition_url": candidate.url,
        "provider_title": candidate.title,
        "provider_artist": candidate.artist,
        "uploader": candidate.uploader_name,
        "uploader_relationship": candidate.uploader_relationship.value,
        "duration_seconds": candidate.duration_seconds,
        "version_signature": candidate.version or "studio",
        "group_key": candidate.identity.stable_key[:500],
        "local_score": 0.0,
        "policy_status": "allowed",
        "probe_status": "valid",
        "contradictions_json": "[]",
        "sanitized_metadata_json": json.dumps(
            {
                "track": candidate.track,
                "artists": list(candidate.artists),
                "artist_source": candidate.artist_source,
                "version": candidate.version,
                "uploader_id": candidate.uploader_id,
                "audio_available": candidate.audio_available,
                "audio_quality": candidate.audio_quality,
                "description_untrusted": candidate.description,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "failure_code": None,
    }


def _domain_from_row(row: DbSourceCandidate) -> SourceCandidate:
    metadata = _json_object(row.sanitized_metadata_json)
    audio_available = metadata.get("audio_available")
    audio_quality = _optional_score(metadata.get("audio_quality"))
    return SourceCandidate(
        source_id=row.source_id,
        provider=ProviderIdentity(row.provider),
        extractor=row.extractor,
        url=_required_db_string(row.acquisition_url, "acquisition_url"),
        title=row.provider_title,
        artist=row.provider_artist,
        artists=structured_artists(metadata.get("artists")),
        artist_source=_optional_artist_source(metadata.get("artist_source")),
        track=_optional_string(metadata.get("track")),
        version=_optional_string(metadata.get("version")) or row.version_signature,
        duration_seconds=row.duration_seconds,
        uploader_name=row.uploader,
        uploader_id=_optional_string(metadata.get("uploader_id")),
        uploader_relationship=UploaderRelationship(row.uploader_relationship),
        audio_available=audio_available if isinstance(audio_available, bool) else True,
        audio_quality=audio_quality if audio_quality is not None else 1.0,
        description=_optional_string(metadata.get("description_untrusted")),
    )


def _resolved(row: DbSourceCandidate) -> ResolvedSource:
    return ResolvedSource(
        candidate_id=row.id,
        url=_required_db_string(row.acquisition_url, "acquisition_url"),
        provider=row.provider,
        extractor=row.extractor,
        source_id=row.source_id,
    )


def _decision_candidate(row: DbSourceCandidate) -> dict[str, object]:
    return {
        "source_candidate_id": row.id,
        "provider": row.provider,
        "extractor": row.extractor,
        "source_id": row.source_id,
        "local_score": row.local_score,
    }


def _review_reason(code: str) -> str:
    return {
        "source_contradiction": "source candidates conflict with the requested recording",
        "duration_not_confirmed": "source duration could not be confirmed safely",
        "local_score_below_threshold": "available sources do not match confidently enough",
        "local_lead_too_small": "materially different sources remain equally plausible",
        "no_eligible_source": "no permitted candidate satisfied source safety policy",
    }.get(code, "source identity could not be resolved confidently")


def _explicit_requested_provider(value: Mapping[str, object]) -> ProviderIdentity | None:
    raw = _optional_string(value.get("requested_provider"))
    if raw is None:
        return None
    try:
        provider = ProviderIdentity(raw)
    except ValueError:
        return None
    return (
        provider
        if provider
        in {
            ProviderIdentity.BANDCAMP,
            ProviderIdentity.SOUNDCLOUD,
            ProviderIdentity.YOUTUBE,
        }
        else None
    )


def _provider_scope_list(value: Mapping[str, object], key: str) -> frozenset[ProviderIdentity]:
    raw_values = value.get(key)
    if not isinstance(raw_values, list):
        return frozenset()
    providers: set[ProviderIdentity] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            continue
        try:
            provider = ProviderIdentity(raw)
        except ValueError:
            continue
        if provider in _ACQUISITION_PROVIDER_SET:
            providers.add(provider)
    return frozenset(providers)


_ACQUISITION_PROVIDER_SET = frozenset(
    {
        ProviderIdentity.BANDCAMP,
        ProviderIdentity.SOUNDCLOUD,
        ProviderIdentity.YOUTUBE,
    }
)


def _requested_provider_scope(value: Mapping[str, object]) -> frozenset[ProviderIdentity]:
    providers = set(_provider_scope_list(value, "requested_providers"))
    requested = _explicit_requested_provider(value)
    if requested is not None:
        providers.add(requested)
    providers.difference_update(_excluded_provider_scope(value))
    return frozenset(providers)


def _excluded_provider_scope(value: Mapping[str, object]) -> frozenset[ProviderIdentity]:
    return _provider_scope_list(value, "excluded_providers")


def _provider_fallback_allowed(value: Mapping[str, object]) -> bool:
    return value.get("provider_fallback_allowed") is True


def _effective_provider_scope(
    value: Mapping[str, object],
) -> frozenset[ProviderIdentity] | None:
    requested = _requested_provider_scope(value)
    excluded = _excluded_provider_scope(value)
    if not requested and not excluded:
        return None
    providers = set(requested or _ACQUISITION_PROVIDER_SET)
    providers.difference_update(excluded)
    if not _provider_fallback_allowed(value):
        return frozenset(providers)
    raw_fallbacks = value.get("provider_fallback_providers")
    if not isinstance(raw_fallbacks, list):
        return frozenset(providers)
    for raw in raw_fallbacks:
        if not isinstance(raw, str):
            continue
        try:
            provider = ProviderIdentity(raw)
        except ValueError:
            continue
        if provider in _ACQUISITION_PROVIDER_SET:
            providers.add(provider)
    return frozenset(providers)


def _first_string(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        result = _optional_string(value.get(key))
        if result is not None:
            return result
    return None


def _required_string(value: Mapping[str, object], key: str) -> str:
    result = _optional_string(value.get(key))
    if result is None:
        raise ValueError(f"approved snapshot is missing {key}")
    return result


def _required_db_string(value: str | None, key: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"source candidate is missing {key}")
    return value


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_artist_source(value: object) -> str | None:
    if isinstance(value, str) and value in {
        "artist",
        "album_artist",
        "creator",
        "parsed_title",
    }:
        return value
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result > 0 else None


def _optional_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return min(1.0, max(0.0, result))


def _json_string_list(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _json_object(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _leased_job(session: Session, lease: JobLease, *, action: str) -> DownloadJob:
    job = session.scalar(
        select(DownloadJob).where(
            DownloadJob.id == lease.job_id,
            DownloadJob.lease_token == lease.token,
            DownloadJob.status == "active",
            DownloadJob.lease_expires_at.is_not(None),
            DownloadJob.lease_expires_at >= datetime.now(UTC),
        )
    )
    if job is None:
        raise LeaseLostError(f"job lease was lost while {action}")
    return job
