from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
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
from app.prompts import SOURCE_MATCH_PROMPT_VERSION
from app.repositories.decisions import candidate_set_fingerprint, record_selected_decision
from app.repositories.jobs import dedup_key
from app.services.artist_credits import artist_credit_variant, structured_artists
from app.services.duplicates import normalize_version_signature
from app.sources import (
    DEFAULT_VERSION_CLASSIFIER,
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
from app.workers.source_discovery_state import CachedSourceSearch, SourceDiscoveryState
from app.workers.source_failures import is_transient_source_error

MAX_EXACT_TRACK_SEARCH_QUERIES = 6
MAX_SEARCH_RESULTS_PER_QUERY = 6
# The ordinary pass is deliberately small. Explicit provider-fallback consent
# unlocks one bounded tranche for only the newly permitted providers; the
# cumulative, crash-safe ledger still caps the entire job at twenty probes.
MAX_SOURCE_DISCOVERY_PROBES = 12
MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK = 20
MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES = (
    MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK - MAX_SOURCE_DISCOVERY_PROBES
)
# Orchestration-time evidence must not consume the entire worker-owned search
# budget. Direct user URLs are already attached as the active candidate, so six
# evidence probes leave six independent probes for deterministic exact search.
MAX_EVIDENCE_DISCOVERY_PROBES = 6
_PERSISTED_PROBE_REJECTION_CODES = frozenset(
    {
        "probe_metadata_invalid",
        "probe_provider_rejected",
        "probe_validation_rejected",
    }
)
_EARLY_STOP_UPLOADER_RELATIONSHIPS = frozenset(
    {
        UploaderRelationship.OFFICIAL_ARTIST,
        UploaderRelationship.OFFICIAL_LABEL,
        UploaderRelationship.TOPIC,
    }
)


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


class EquivalentAcquisitionActive(RuntimeError):
    """The corrected recording identity is already being acquired by another job."""


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
        intent = SourceIntent(
            artist=_required_string(lease.approved_snapshot, "artist"),
            artists=structured_artists(lease.approved_snapshot.get("artists")),
            title=_required_string(lease.approved_snapshot, "title"),
            requested_version=_requested_source_version(
                lease.approved_snapshot,
                self.settings.default_version_preference,
            ),
            duration_seconds=_optional_float(lease.approved_snapshot.get("duration_seconds")),
        )
        self._repair_inferred_version_snapshot(lease, intent)
        active = self._active_candidate(lease, intent)
        if active is not None:
            return active
        provider_scope = _effective_provider_scope(lease.approved_snapshot)
        candidates = self._discover(
            lease,
            intent,
            cancellation,
            monitor,
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

    def _active_candidate(self, lease: JobLease, intent: SourceIntent) -> ResolvedSource | None:
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
            evidence = session.get(EvidenceReference, row.evidence_id) if row.evidence_id else None
            selected_decisions = list(
                session.scalars(
                    select(JobDecision)
                    .where(
                        JobDecision.job_id == job.id,
                        JobDecision.category == "acquisition_source",
                        JobDecision.state == "selected",
                    )
                    .order_by(JobDecision.revision.desc(), JobDecision.id.desc())
                    .limit(20)
                )
            )
            existing_decision = next(
                (
                    decision
                    for decision in selected_decisions
                    if _decision_selects_source(decision, row.id)
                ),
                None,
            )
            user_selected = existing_decision is not None and existing_decision.decided_by == "user"
            direct_source = bool(
                evidence is not None and evidence.evidence_kind == "direct_user_url"
            )
            if existing_decision is None and not direct_source:
                # Queue insertion may attach a locally allowed orchestration
                # candidate as a convenience. Natural-language evidence is not a
                # durable acquisition decision and must pass current intent ranking.
                job.active_source_candidate_id = None
                job.source_extractor = None
                job.source_id = None
                return None
            candidate_version = DEFAULT_VERSION_CLASSIFIER.classify_signature(row.version_signature)
            requested_version = DEFAULT_VERSION_CLASSIFIER.classify_signature(
                intent.requested_version
            )
            if (
                not user_selected
                and not direct_source
                and not DEFAULT_VERSION_CLASSIFIER.compatible(requested_version, candidate_version)
            ):
                # A pre-upgrade automatic decision may have inherited "live"
                # from a compilation title.  Fence and retire it before the
                # corrected recording intent is used for bounded rediscovery.
                row.policy_status = "exhausted"
                row.failure_code = "inferred_version_revalidated"
                row.attempted_at = datetime.now(UTC)
                if existing_decision is not None:
                    existing_decision.state = "rejected"
                    reasons = _json_string_list(existing_decision.reason_codes_json)
                    reasons.append("inferred_version_revalidated")
                    existing_decision.reason_codes_json = json.dumps(
                        list(dict.fromkeys(reasons)), separators=(",", ":")
                    )
                job.active_source_candidate_id = None
                job.source_extractor = None
                job.source_id = None
                return None
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

    def _repair_inferred_version_snapshot(self, lease: JobLease, intent: SourceIntent) -> None:
        """Repair only durable release/model inference, never recording evidence."""

        snapshot = lease.approved_snapshot
        prior = normalize_version_signature(_optional_string(snapshot.get("version_signature")))
        requested = normalize_version_signature(intent.requested_version)
        if (
            _source_version_is_explicit(snapshot)
            or not _release_only_version_inference(snapshot)
            or DEFAULT_VERSION_CLASSIFIER.compatible(
                DEFAULT_VERSION_CLASSIFIER.classify_signature(prior),
                DEFAULT_VERSION_CLASSIFIER.classify_signature(requested),
            )
            or DEFAULT_VERSION_CLASSIFIER.classify_recording(
                _optional_string(snapshot.get("title"))
            ).kinds
        ):
            return
        corrected = dict(snapshot)
        corrected["version_signature"] = requested
        provenance = corrected.get("metadata_provenance")
        safe_provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
        safe_provenance["recording_version_correction"] = {
            "reason_code": "release_context_not_recording_version",
            "from": prior[:100],
            "to": requested[:100],
        }
        corrected["metadata_provenance"] = safe_provenance
        with self.factory() as session:
            # Re-keying participates in the active-job uniqueness invariant. Take
            # the SQLite writer reservation before checking for a conflicting key
            # so another queue insertion cannot race the correction.
            session.execute(text("BEGIN IMMEDIATE"))
            job = _leased_job(session, lease, action="repairing inferred recording version")
            track = session.get(RequestTrack, job.request_track_id)
            if track is None:
                session.rollback()
                raise LookupError(job.request_track_id)
            track_version = normalize_version_signature(track.version_signature)
            if track_version not in {prior, requested}:
                session.rollback()
                raise LeaseLostError(
                    "request track changed while repairing inferred recording version"
                )
            track.version_signature = requested
            replacement_key = dedup_key(track)
            conflicting_job = session.scalar(
                select(DownloadJob.id).where(
                    DownloadJob.id != job.id,
                    DownloadJob.dedup_key == replacement_key,
                    DownloadJob.status.not_in(("cancelled", "failed", "completed")),
                )
            )
            if conflicting_job is not None:
                session.rollback()
                raise EquivalentAcquisitionActive(
                    "an equivalent corrected acquisition is already active"
                )
            job.approved_snapshot_json = json.dumps(
                corrected, ensure_ascii=False, separators=(",", ":")
            )
            job.dedup_key = replacement_key
            track.metadata_provenance_json = json.dumps(
                safe_provenance, ensure_ascii=False, separators=(",", ":")
            )
            session.commit()
        snapshot.clear()
        snapshot.update(corrected)

    def _discover(
        self,
        lease: JobLease,
        intent: SourceIntent,
        cancellation: CancellationSignal,
        monitor: LeaseMonitor,
        *,
        provider_scope: frozenset[ProviderIdentity] | None,
    ) -> tuple[SourceCandidate, ...]:
        request_track_id = _required_string(lease.approved_snapshot, "request_track_id")
        request_id, request_track_count = self._request_context(request_track_id)
        consented_fallback_scope = frozenset(
            provider
            for provider in _consented_fallback_provider_scope(lease.approved_snapshot)
            if provider in self.policy.allowed_providers
        )
        probe_limit = MAX_SOURCE_DISCOVERY_PROBES
        probe_epoch: str | None = None
        probe_epoch_limit: int | None = None
        if consented_fallback_scope:
            # The requested provider has already completed its bounded pass before
            # the user was asked. Spend the additional tranche only on providers
            # that the user has now explicitly permitted.
            provider_scope = consented_fallback_scope
            probe_limit = MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK
            probe_epoch = "provider_fallback"
            probe_epoch_limit = MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES
        discovery_state = SourceDiscoveryState(
            self.factory,
            lease,
            request_id=request_id,
            request_track_id=request_track_id,
            policy_fingerprint=_source_search_policy_fingerprint(self.policy),
            max_diagnostic_runs=self.policy.max_attempts,
        )
        if request_track_count == 1:
            self._adopt_request_evidence(request_id, request_track_id, lease)
        blocked = self._blocked_identity_keys(request_track_id)
        existing = tuple(
            candidate
            for candidate in self._existing_domain_candidates(request_id, request_track_id, lease)
            if provider_scope is None or candidate.provider in provider_scope
        )
        discovered: list[SourceCandidate] = list(existing)
        probe_count = discovery_state.probe_total()
        searched_queries: set[str] = set()
        query_attempts: list[dict[str, object]] = []
        found_count = 0
        rejection_counts: dict[str, int] = {}
        transient_error: SourceValidationError | YtDlpError | None = None
        transient_count = 0
        attempted_evidence_identities: set[str] = set()
        if not _has_early_stop_source(intent, discovered, self.policy):
            needed = max(0, self.policy.max_candidates - len(discovered))
            if needed:
                (
                    evidence_candidates,
                    _evidence_probe_count,
                    attempted_evidence_identities,
                    evidence_transient_error,
                    evidence_transient_count,
                ) = self._evidence_candidates(
                    request_id,
                    request_track_id,
                    lease,
                    min(needed, MAX_EVIDENCE_DISCOVERY_PROBES),
                    cancellation,
                    monitor,
                    excluded={item.identity.stable_key for item in discovered} | blocked,
                    provider_scope=provider_scope,
                    intent=intent,
                    probe_budget=MAX_EVIDENCE_DISCOVERY_PROBES,
                    total_probe_limit=probe_limit,
                    probe_epoch=probe_epoch,
                    probe_epoch_limit=probe_epoch_limit,
                    discovery_state=discovery_state,
                )
                discovered.extend(evidence_candidates)
                probe_count = discovery_state.probe_total()
                transient_error = evidence_transient_error
                transient_count = evidence_transient_count
                if evidence_transient_count:
                    rejection_counts["transient_evidence_probe"] = min(
                        1_000, evidence_transient_count
                    )
        seen = (
            {item.identity.stable_key for item in discovered}
            | blocked
            | attempted_evidence_identities
        )
        stop = _has_early_stop_source(intent, discovered, self.policy)
        queries = _exact_track_search_queries(intent)
        # Query-first ordering tries the strongest phrase across every permitted
        # provider before progressively broadening it. This prevents one provider's
        # weaker variants from delaying an exact result on the next provider. Each
        # query gets a fair share of the remaining probe budget; unused shares roll
        # forward, and a validated official source stops later searches immediately.
        for query_index, query in enumerate(queries):
            if stop or len(discovered) >= self.policy.max_candidates:
                break
            if cancellation.is_set():
                raise InterruptedError("source discovery was cancelled")
            monitor.raise_if_unusable()
            query_candidates: list[SourceCandidate] = []
            query_seen: set[str] = set()
            for provider in self.policy.provider_preference:
                if stop or len(discovered) >= self.policy.max_candidates:
                    break
                if provider not in self.policy.allowed_providers:
                    continue
                if provider_scope is not None and provider not in provider_scope:
                    continue
                if provider is ProviderIdentity.BANDCAMP:
                    continue
                candidate_room = self.policy.max_candidates - len(discovered)
                if candidate_room <= 0 or probe_count >= probe_limit:
                    stop = True
                    break
                limit = MAX_SEARCH_RESULTS_PER_QUERY
                searched_queries.add(query)
                cached_search = discovery_state.cached_search(
                    provider,
                    query,
                    maximum_candidates=MAX_SEARCH_RESULTS_PER_QUERY,
                )
                if cached_search is None:
                    try:
                        payload = self.ytdlp.search_provider(
                            query,
                            provider=provider,
                            limit=limit,
                            cancel_signal=cancellation,
                        )
                    except (SourceValidationError, YtDlpError) as exc:
                        _record_query_attempt(query_attempts, provider, query, found_count=0)
                        _raise_if_discovery_interrupted(exc, cancellation, monitor)
                        if is_transient_source_error(exc):
                            transient_error = transient_error or exc
                            transient_count = min(1_000, transient_count + 1)
                            _increment(rejection_counts, "transient_provider_search")
                            continue
                        _increment(rejection_counts, "provider_search_rejected")
                        continue
                    entries = payload.get("entries")
                    if not isinstance(entries, list):
                        _record_query_attempt(query_attempts, provider, query, found_count=0)
                        _increment(rejection_counts, "malformed_search_result")
                        continue
                    bounded_entries = entries[:limit]
                    flat_candidates: list[SourceCandidate] = []
                    invalid_count = 0
                    for entry in bounded_entries:
                        flat = _candidate_from_metadata(provider, entry)
                        if flat is None:
                            invalid_count += 1
                        else:
                            flat_candidates.append(flat)
                    cached_search = CachedSourceSearch(
                        candidates=tuple(flat_candidates),
                        found_count=len(bounded_entries),
                        invalid_count=invalid_count,
                    )
                    discovery_state.store_search(provider, query, cached_search)
                _record_query_attempt(
                    query_attempts,
                    provider,
                    query,
                    found_count=cached_search.found_count,
                )
                found_count += cached_search.found_count
                for _index in range(cached_search.invalid_count):
                    _increment(rejection_counts, "invalid_flat_candidate")
                for flat in cached_search.candidates:
                    if cancellation.is_set():
                        raise InterruptedError("source discovery was cancelled")
                    monitor.raise_if_unusable()
                    if flat.identity.stable_key in seen or flat.identity.stable_key in query_seen:
                        _increment(rejection_counts, "duplicate_source_id")
                        continue
                    query_seen.add(flat.identity.stable_key)
                    query_candidates.append(flat)

            remaining_probe_budget = max(0, probe_limit - probe_count)
            remaining_queries = len(queries) - query_index
            query_probe_budget = min(
                remaining_probe_budget,
                max(1, math.ceil(remaining_probe_budget / remaining_queries)),
            )
            # Flat ranking is advisory only: every candidate selected from this
            # query is still fully probed and validated before it can stop discovery.
            ranked_query = _rank_discovery_candidates(intent, query_candidates, self.policy)
            prior_attempts = discovery_state.probe_attempt_counts(
                [item.candidate.identity.stable_key for item in ranked_query]
            )
            # After restart, continue through untried cached results before using
            # the one bounded transient retry for an identity already probed.
            ordered_query = sorted(
                ranked_query,
                key=lambda item: prior_attempts.get(item.candidate.identity.stable_key, 0),
            )
            for item in ordered_query[:query_probe_budget]:
                if stop or len(discovered) >= self.policy.max_candidates:
                    break
                if cancellation.is_set():
                    raise InterruptedError("source discovery was cancelled")
                monitor.raise_if_unusable()
                flat = item.candidate
                # Mark the identity only when it spends a probe. An unprobed result
                # may reappear with stronger metadata in a later official query.
                reservation = discovery_state.reserve_probe(
                    flat.identity.stable_key,
                    maximum_total=probe_limit,
                    epoch=probe_epoch,
                    maximum_epoch=probe_epoch_limit,
                )
                if not reservation.reserved:
                    seen.add(flat.identity.stable_key)
                    if reservation.remaining == 0:
                        stop = True
                        _increment(rejection_counts, "probe_budget_exhausted")
                        break
                    continue
                seen.add(flat.identity.stable_key)
                probe_count = reservation.total
                try:
                    full = self.ytdlp.probe(flat.url, cancel_signal=cancellation)
                    candidate = _candidate_from_metadata(flat.provider, full, fallback=flat)
                except (SourceValidationError, YtDlpError) as exc:
                    _raise_if_discovery_interrupted(exc, cancellation, monitor)
                    if is_transient_source_error(exc):
                        transient_error = transient_error or exc
                        transient_count = min(1_000, transient_count + 1)
                        _increment(rejection_counts, "transient_candidate_probe")
                        continue
                    reason_code, probe_status = _probe_rejection(exc)
                    self._persist_probe_rejection(
                        request_track_id,
                        lease,
                        flat,
                        reason_code=reason_code,
                        probe_status=probe_status,
                    )
                    _increment(rejection_counts, "probe_rejected")
                    continue
                if candidate is None:
                    self._persist_probe_rejection(
                        request_track_id,
                        lease,
                        flat,
                        reason_code="probe_metadata_invalid",
                        probe_status="invalid",
                    )
                    _increment(rejection_counts, "invalid_probe_candidate")
                elif candidate.identity.stable_key in blocked:
                    _increment(rejection_counts, "exhausted_source_id")
                else:
                    discovered.append(candidate)
                    if _has_early_stop_source(intent, [candidate], self.policy):
                        stop = True
                        break
        unique = {candidate.identity.stable_key: candidate for candidate in discovered}
        bounded = tuple(
            item.candidate
            for item in _rank_discovery_candidates(intent, tuple(unique.values()), self.policy)[
                : self.policy.max_candidates
            ]
        )
        diagnostics = {
            "schema_version": 1,
            "query_variant_count": len(searched_queries),
            "query_attempts": query_attempts,
            "found_count": min(found_count, 1_000),
            "probed_count": probe_count,
            "accepted_count": len(bounded),
            "rejection_counts": dict(sorted(rejection_counts.items())),
            "stopped_early": stop and probe_count < probe_limit,
        }
        self._persist_candidates(
            request_track_id,
            lease,
            bounded,
            diagnostics=diagnostics,
        )
        discovery_state.persist_diagnostics(diagnostics)
        if transient_count:
            self.queue.add_warning(
                lease,
                code="source_discovery_transient",
                message=(
                    f"{transient_count} temporary media source check(s) failed; "
                    "other safe alternatives were attempted."
                ),
            )
        if transient_error is not None and (
            not bounded
            or decide_source_match(intent, bounded, policy=self.policy).decision
            is not MatchDecision.MATCH
        ):
            # A weak or contradictory candidate is not a successful fallback: a
            # temporarily unavailable provider may still contain the correct item.
            # Preserve the inspected candidates/diagnostics and retry instead of
            # turning that outage into an exceptional user review.
            raise transient_error
        return bounded

    def _evidence_candidates(
        self,
        request_id: str,
        request_track_id: str,
        lease: JobLease,
        limit: int,
        cancellation: CancellationSignal,
        monitor: LeaseMonitor,
        *,
        excluded: set[str],
        provider_scope: frozenset[ProviderIdentity] | None,
        intent: SourceIntent,
        probe_budget: int,
        total_probe_limit: int,
        probe_epoch: str | None,
        probe_epoch_limit: int | None,
        discovery_state: SourceDiscoveryState,
    ) -> tuple[
        list[SourceCandidate],
        int,
        set[str],
        SourceValidationError | YtDlpError | None,
        int,
    ]:
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
        probe_count = 0
        attempted_identities: set[str] = set()
        transient_error: SourceValidationError | YtDlpError | None = None
        transient_count = 0
        eligible_evidence: list[
            tuple[EvidenceReference, ProviderIdentity, SourceCandidate | None, str]
        ] = []
        for reference in evidence:
            provider = provider_for_url(reference.canonical_url or "")
            if provider not in self.policy.allowed_providers:
                continue
            if provider_scope is not None and provider not in provider_scope:
                continue
            candidate_stub = _candidate_from_evidence_reference(provider, reference)
            identity = (
                candidate_stub.identity.stable_key
                if candidate_stub is not None
                else (
                    f"{provider.value}:url:"
                    f"{hashlib.sha256((reference.canonical_url or '').encode()).hexdigest()}"
                )
            )
            eligible_evidence.append((reference, provider, candidate_stub, identity))
        evidence_attempts = discovery_state.probe_attempt_counts(
            [identity for _reference, _provider, _stub, identity in eligible_evidence]
        )
        eligible_evidence.sort(key=lambda item: evidence_attempts.get(item[3], 0))
        for reference, provider, candidate_stub, identity in eligible_evidence:
            if cancellation.is_set():
                raise InterruptedError("source discovery was cancelled")
            monitor.raise_if_unusable()
            if reference.provider_item_id:
                identity_key = (
                    f"{provider.value}:{provider_capability(provider).canonical_extractor}:"
                    f"{reference.provider_item_id}"
                )
                if identity_key in excluded:
                    continue
                # A failed evidence probe must not be repeated when the same
                # stable provider/extractor/ID appears in a later search result.
                attempted_identities.add(identity_key)
            try:
                if probe_count >= probe_budget:
                    break
                reservation = discovery_state.reserve_probe(
                    identity,
                    maximum_total=total_probe_limit,
                    epoch=probe_epoch,
                    maximum_epoch=probe_epoch_limit,
                )
                if not reservation.reserved:
                    if reservation.remaining == 0:
                        break
                    continue
                probe_count += 1
                metadata = self.ytdlp.probe(
                    reference.canonical_url or "", cancel_signal=cancellation
                )
            except (SourceValidationError, YtDlpError) as exc:
                _raise_if_discovery_interrupted(exc, cancellation, monitor)
                if is_transient_source_error(exc):
                    transient_error = transient_error or exc
                    transient_count = min(1_000, transient_count + 1)
                    continue
                reason_code, probe_status = _probe_rejection(exc)
                self._persist_probe_rejection(
                    request_track_id,
                    lease,
                    candidate_stub,
                    evidence_id=reference.id,
                    reason_code=reason_code,
                    probe_status=probe_status,
                )
                continue
            candidate = _candidate_from_metadata(provider, metadata)
            if candidate is None:
                self._persist_probe_rejection(
                    request_track_id,
                    lease,
                    candidate_stub,
                    evidence_id=reference.id,
                    reason_code="probe_metadata_invalid",
                    probe_status="invalid",
                )
            elif candidate.identity.stable_key not in excluded:
                attempted_identities.add(candidate.identity.stable_key)
                result.append(candidate)
                if len(result) >= limit or _has_early_stop_source(intent, [candidate], self.policy):
                    break
        return (
            result,
            probe_count,
            attempted_identities,
            transient_error,
            transient_count,
        )

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

    def _blocked_identity_keys(self, request_track_id: str) -> set[str]:
        with self.factory() as session:
            rows = session.execute(
                select(
                    DbSourceCandidate.provider,
                    DbSourceCandidate.extractor,
                    DbSourceCandidate.source_id,
                ).where(
                    DbSourceCandidate.request_track_id == request_track_id,
                    or_(
                        DbSourceCandidate.policy_status == "exhausted",
                        and_(
                            DbSourceCandidate.policy_status == "rejected",
                            DbSourceCandidate.probe_status.in_(["invalid", "failed"]),
                            DbSourceCandidate.failure_code.in_(
                                sorted(_PERSISTED_PROBE_REJECTION_CODES)
                            ),
                        ),
                    ),
                )
            )
            return {
                f"{provider}:{extractor}:{source_id}" for provider, extractor, source_id in rows
            }

    def _persist_probe_rejection(
        self,
        request_track_id: str,
        lease: JobLease,
        candidate: SourceCandidate | None,
        *,
        reason_code: str,
        probe_status: str,
        evidence_id: str | None = None,
    ) -> None:
        """Persist a permanent, identity-scoped rejection without raw error text."""

        if reason_code not in _PERSISTED_PROBE_REJECTION_CODES:
            raise ValueError("unsupported persisted probe rejection")
        with self.factory.begin() as session:
            _leased_job(session, lease, action="persisting a rejected source probe")
            if evidence_id is not None:
                evidence = session.get(EvidenceReference, evidence_id)
                if evidence is not None:
                    evidence.status = "rejected"
                    evidence.negative_reason = reason_code
                    evidence.negative_until = None
            if candidate is None:
                return
            row = session.scalar(
                select(DbSourceCandidate).where(
                    DbSourceCandidate.request_track_id == request_track_id,
                    DbSourceCandidate.provider == candidate.provider.value,
                    DbSourceCandidate.extractor == candidate.extractor,
                    DbSourceCandidate.source_id == candidate.source_id,
                )
            )
            if row is not None and (
                row.policy_status == "exhausted" or row.probe_status == "valid"
            ):
                return
            values = _db_candidate_values(candidate)
            values.update(
                {
                    "evidence_id": evidence_id,
                    "job_id": lease.job_id,
                    "policy_status": "rejected",
                    "probe_status": probe_status,
                    "failure_code": reason_code,
                    "attempted_at": datetime.now(UTC),
                }
            )
            if row is None:
                session.add(DbSourceCandidate(request_track_id=request_track_id, **values))
                return
            for key, value in values.items():
                setattr(row, key, value)

    def _persist_candidates(
        self,
        request_track_id: str,
        lease: JobLease,
        candidates: tuple[SourceCandidate, ...],
        *,
        diagnostics: Mapping[str, object] | None = None,
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
                metadata = _json_object(str(values["sanitized_metadata_json"]))
                if row is not None:
                    # Matching-domain rows intentionally omit operational
                    # provenance. Preserve it across retries and restarts.
                    metadata = {
                        **_json_object(row.sanitized_metadata_json),
                        **metadata,
                    }
                if diagnostics is not None:
                    previous = metadata.get("discovery_diagnostics")
                    if not (
                        isinstance(previous, Mapping)
                        and previous
                        and not _discovery_diagnostics_has_new_work(diagnostics)
                    ):
                        metadata["discovery_diagnostics"] = dict(diagnostics)
                values["sanitized_metadata_json"] = json.dumps(
                    metadata, ensure_ascii=False, separators=(",", ":")
                )
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
                prompt_version=(SOURCE_MATCH_PROMPT_VERSION if authority == "openai" else None),
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
            "matcher_prompt_version": SOURCE_MATCH_PROMPT_VERSION,
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
                payload_version=3,
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
    # Missing bitrate is neutral, not maximum quality evidence. Full
    # format choice and byte/codec validation still happen before acquisition.
    quality = 0.5 if abr is None else min(1.0, max(0.1, abr / 256.0))
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


def _candidate_from_evidence_reference(
    provider: ProviderIdentity,
    reference: EvidenceReference,
) -> SourceCandidate | None:
    """Build the smallest safe identity record for a permanently failed probe."""

    source_id = _optional_string(reference.provider_item_id)
    url = _optional_string(reference.canonical_url)
    if source_id is None or url is None:
        return None
    metadata = _json_object(reference.sanitized_metadata_json)
    title = _optional_string(metadata.get("title")) or "Rejected source candidate"
    try:
        return SourceCandidate(
            source_id=source_id,
            provider=provider,
            extractor=provider_capability(provider).canonical_extractor,
            url=url,
            title=title[:500],
            audio_available=False,
            audio_quality=0.0,
        )
    except ValueError:
        return None


def _probe_rejection(exc: SourceValidationError | YtDlpError) -> tuple[str, str]:
    """Map provider failures to bounded codes; never persist exception text."""

    if isinstance(exc, SourceValidationError):
        return "probe_validation_rejected", "invalid"
    return "probe_provider_rejected", "failed"


def _raise_if_discovery_interrupted(
    exc: SourceValidationError | YtDlpError,
    cancellation: CancellationSignal,
    monitor: LeaseMonitor,
) -> None:
    """Never reinterpret process cancellation or a lost lease as provider failure."""

    if isinstance(exc, DownloadCancelled):
        raise exc
    if cancellation.is_set():
        raise InterruptedError("source discovery was cancelled") from exc
    monitor.raise_if_unusable()


def _rank_discovery_candidates(
    intent: SourceIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    policy: SourcePolicy,
) -> tuple[Any, ...]:
    """Rank the bounded discovery superset before applying the persisted cap."""

    if not candidates:
        return ()
    discovery_policy = policy.model_copy(
        update={"max_candidates": min(100, max(policy.max_candidates, len(candidates)))}
    )
    return rank_sources(intent, candidates[:100], policy=discovery_policy)


def _source_search_policy_fingerprint(policy: SourcePolicy) -> str:
    """Invalidate job-local flat searches when matching policy semantics change."""

    material = json.dumps(
        {
            "schema_version": 1,
            "recording_version_policy": "recording_version_v2",
            "source_match_prompt": SOURCE_MATCH_PROMPT_VERSION,
            "policy": policy.model_dump(mode="json"),
            "query_limit": MAX_SEARCH_RESULTS_PER_QUERY,
            "initial_probe_limit": MAX_SOURCE_DISCOVERY_PROBES,
            "fallback_probe_limit": MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _requested_source_version(snapshot: Mapping[str, object], default: str) -> str:
    """Use user intent, then recording-level provider evidence, then policy.

    ``version_signature`` can originate in model proposal text and therefore is
    not authoritative by itself for unattended source selection. Direct and
    collection ingestion record a separate durable provenance marker after a
    policy-validated provider probe; that recording-level evidence is safe to
    retain across the later source and canonical-metadata stages.
    """

    requested = _optional_string(snapshot.get("requested_version"))
    raw_explicit = snapshot.get("version_constraint_explicit")
    explicit: bool | None = raw_explicit if isinstance(raw_explicit, bool) else None
    provenance = snapshot.get("metadata_provenance")
    if explicit is None and isinstance(provenance, Mapping):
        for key in ("request_constraints", "user_constraints"):
            constraints = provenance.get(key)
            marker = (
                constraints.get("version_constraint_explicit")
                if isinstance(constraints, Mapping)
                else None
            )
            if isinstance(marker, bool):
                explicit = marker
                break
    # A value explicitly marked as inferred is only a proposal hint. Keeping it
    # would reject the exact studio source as (for example) an unwanted live cut.
    if explicit is True:
        return normalize_version_signature(
            requested or _optional_string(snapshot.get("version_signature")) or default
        )
    durable_version = _durable_recording_version_constraint(snapshot)
    if durable_version is not None:
        return durable_version
    if explicit is False:
        return normalize_version_signature(default)
    return normalize_version_signature(requested or default)


def _source_version_is_explicit(snapshot: Mapping[str, object]) -> bool:
    raw_explicit = snapshot.get("version_constraint_explicit")
    if isinstance(raw_explicit, bool):
        return raw_explicit
    provenance = snapshot.get("metadata_provenance")
    if not isinstance(provenance, Mapping):
        return False
    for key in ("user_constraints", "request_constraints"):
        constraints = provenance.get(key)
        if isinstance(constraints, Mapping):
            marker = constraints.get("version_constraint_explicit")
            if isinstance(marker, bool):
                return marker
    return provenance.get("version_constraint_explicit") is True


def _durable_recording_version_constraint(snapshot: Mapping[str, object]) -> str | None:
    """Return a version only when durable provenance names recording evidence."""

    provenance = snapshot.get("metadata_provenance")
    if not isinstance(provenance, Mapping):
        return None
    recording_version = provenance.get("recording_version")
    if not isinstance(recording_version, Mapping):
        return None
    if recording_version.get("source") not in {
        "musicbrainz_recording_disambiguation",
        "provider_recording_metadata",
    }:
        return None
    value = _optional_string(recording_version.get("signature"))
    if value is None:
        return None
    return normalize_version_signature(value)


def _release_only_version_inference(snapshot: Mapping[str, object]) -> bool:
    """Identify legacy special versions known to come from non-recording text.

    Missing provenance is deliberately not repaired. That conservative default
    protects genuine MusicBrainz disambiguation and provider-evidenced live or
    remix recordings whose canonical title itself is plain.
    """

    provenance = snapshot.get("metadata_provenance")
    if not isinstance(provenance, Mapping):
        return False
    if provenance.get("source") == "unverified_model_output":
        return True
    recording_version = provenance.get("recording_version")
    if isinstance(recording_version, Mapping):
        return recording_version.get("source") in {
            "model_inference",
            "model_release_context",
            "release_context",
            "release_metadata",
            "release_only",
        }

    # Releases built by 9b8cc17 used ``item.version, item.title, item.album``
    # to fill version_signature. They can be identified without guessing from
    # arbitrary old rows: the verified MB provenance predates both structured
    # artist credits and the recording-version marker, and the album alone
    # reproduces the stored special signature while the recording title does not.
    if not (
        provenance.get("source") == "musicbrainz_search_recordings"
        and provenance.get("automatic_association") is True
        and snapshot.get("canonical_identity_verified") is True
        and "artists" not in provenance
    ):
        return False
    prior = normalize_version_signature(_optional_string(snapshot.get("version_signature")))
    if prior == "studio":
        return False
    title = _optional_string(snapshot.get("title"))
    album = _optional_string(snapshot.get("album"))
    return (
        not DEFAULT_VERSION_CLASSIFIER.classify_recording(title).kinds
        and normalize_version_signature(album) == prior
    )


def _exact_track_search_queries(intent: SourceIntent) -> tuple[str, ...]:
    """Return a deterministic, finite exact-track search cascade."""

    artist = _search_piece(intent.artist)
    title = _search_piece(intent.title)
    credit_variant = _artist_credit_search_variant(artist, intent.artists)
    variants = [
        f"{artist} {title}",
    ]
    if credit_variant != artist:
        variants.append(f"{credit_variant} {title}")
    variants.extend(
        [
            f"{title} {artist}",
        ]
    )
    requested = DEFAULT_VERSION_CLASSIFIER.classify_signature(intent.requested_version)
    if requested.kinds:
        variants.append(f"{artist} {title} {_search_piece(intent.requested_version, limit=40)}")
    variants.extend(
        [
            f"{artist} {title} official audio",
            f"{artist} {title} official video",
            f"{artist} - {title}",
            f'"{artist}" "{title}"',
        ]
    )
    unique: list[str] = []
    for value in variants:
        normalized = " ".join(value.split())[:300].strip()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return tuple(unique[:MAX_EXACT_TRACK_SEARCH_QUERIES])


def _search_piece(value: str, *, limit: int = 125) -> str:
    # Quotes are generated by this module for one bounded phrase variant. User
    # punctuation remains data and cannot reshape that search operator.
    return " ".join(value.replace('"', " ").split())[:limit].strip()


def _artist_credit_search_variant(value: str, artists: tuple[str, ...] = ()) -> str:
    """Normalize collaboration punctuation only as a query, never as identity."""

    # A mixed comma/ampersand credit such as "Earth, Wind & Fire" is too
    # ambiguous to reinterpret. Structured provider artist arrays remain the
    # authoritative way to represent actual collaborators.
    if len(artists) > 1:
        return _search_piece(" & ".join(artists))
    if "," in value and "&" in value:
        return value
    variant = artist_credit_variant(value)
    if variant == value and "&" in value:
        variant = value.replace("&", " and ")
    return _search_piece(variant)


def _increment(counts: dict[str, int], reason: str) -> None:
    counts[reason] = min(1_000, counts.get(reason, 0) + 1)


def _record_query_attempt(
    attempts: list[dict[str, object]],
    provider: ProviderIdentity,
    query: str,
    *,
    found_count: int,
) -> None:
    if len(attempts) >= 12:
        return
    attempts.append(
        {
            "provider": provider.value,
            "query": query[:300],
            "found_count": min(MAX_SEARCH_RESULTS_PER_QUERY, max(0, found_count)),
        }
    )


def _discovery_diagnostics_has_new_work(value: Mapping[str, object]) -> bool:
    attempts = value.get("query_attempts")
    if isinstance(attempts, list) and any(
        isinstance(attempt, Mapping)
        and isinstance(attempt.get("found_count"), int)
        and not isinstance(attempt.get("found_count"), bool)
        and attempt["found_count"] > 0
        for attempt in attempts
    ):
        return True
    for key in ("found_count", "probed_count"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return True
    rejections = value.get("rejection_counts")
    return isinstance(rejections, Mapping) and bool(rejections)


def _has_early_stop_source(
    intent: SourceIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    policy: SourcePolicy,
) -> bool:
    """Stop discovery only for a validated high-confidence official source.

    A third-party exact match remains eligible for final automatic selection, but
    does not prevent the bounded official-audio/video queries from finding a
    preferable equivalent upload.
    """

    return any(
        candidate.uploader_relationship in _EARLY_STOP_UPLOADER_RELATIONSHIPS
        and decide_source_match(intent, [candidate], policy=policy).decision is MatchDecision.MATCH
        for candidate in candidates
    )


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
    recording_version = DEFAULT_VERSION_CLASSIFIER.classify_recording(
        candidate.title,
        candidate.track,
        explicit_version=candidate.version,
    ).signature
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
        "version_signature": normalize_version_signature(recording_version),
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
        audio_quality=audio_quality if audio_quality is not None else 0.5,
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


def _decision_selects_source(decision: JobDecision, candidate_id: str) -> bool:
    payload = _json_object(decision.selected_payload_json or "{}")
    return payload.get("source_candidate_id") == candidate_id


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


def _consented_fallback_provider_scope(
    value: Mapping[str, object],
) -> frozenset[ProviderIdentity]:
    """Return only newly consented providers from a valid constrained request."""

    if not _provider_fallback_allowed(value):
        return frozenset()
    requested = _requested_provider_scope(value)
    excluded = _excluded_provider_scope(value)
    if not requested and not excluded:
        return frozenset()
    initial_scope = set(requested or _ACQUISITION_PROVIDER_SET)
    initial_scope.difference_update(excluded)
    fallbacks = _provider_scope_list(value, "provider_fallback_providers")
    return frozenset(provider for provider in fallbacks if provider not in initial_scope)


def _effective_provider_scope(
    value: Mapping[str, object],
) -> frozenset[ProviderIdentity] | None:
    requested = _requested_provider_scope(value)
    excluded = _excluded_provider_scope(value)
    if not requested and not excluded:
        return None
    providers = set(requested or _ACQUISITION_PROVIDER_SET)
    providers.difference_update(excluded)
    providers.update(_consented_fallback_provider_scope(value))
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
