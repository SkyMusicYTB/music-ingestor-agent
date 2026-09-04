"""Durable, worker-only state for bounded media-source discovery."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DownloadJob, EvidenceReference
from app.repositories.cache import ExternalCacheRepository
from app.sources import SourceCandidate, provider_capability, provider_for_url
from app.sources.identities import ProviderIdentity
from app.workers.queue import JobLease, LeaseLostError

SOURCE_DISCOVERY_DIAGNOSTICS_KIND = "source_search_diagnostics"
SOURCE_SEARCH_CACHE_NAMESPACE = "job-source-search-v1"
SOURCE_SEARCH_CACHE_SCHEMA_VERSION = 1
SOURCE_SEARCH_CACHE_TTL = timedelta(hours=24)
MAX_PROBE_ATTEMPTS_PER_IDENTITY = 2
MAX_DIAGNOSTIC_RUNS = 10
MAX_RECORDED_SOURCE_DISCOVERY_PROBES = 20
_DIAGNOSTIC_REJECTION_CODES = frozenset(
    {
        "provider_search_rejected",
        "transient_provider_search",
        "transient_evidence_probe",
        "transient_candidate_probe",
        "malformed_search_result",
        "invalid_flat_candidate",
        "duplicate_source_id",
        "probe_rejected",
        "invalid_probe_candidate",
        "exhausted_source_id",
        "probe_budget_exhausted",
    }
)
_DIAGNOSTIC_PROVIDERS = frozenset({"bandcamp", "soundcloud", "youtube"})
_DIAGNOSTIC_QUERY_SECRET = re.compile(
    r"(?:https?://|\b(?:authorization|bearer|cookie|password|api[_ -]?key|token)\s*[:=])",
    re.IGNORECASE,
)


class _ProbeLedger(TypedDict):
    total: int
    counts: dict[str, int]
    epochs: dict[str, int]


@dataclass(frozen=True, slots=True)
class CachedSourceSearch:
    """One successful, sanitized provider search result (including empty results)."""

    candidates: tuple[SourceCandidate, ...]
    found_count: int
    invalid_count: int


@dataclass(frozen=True, slots=True)
class ProbeReservation:
    reserved: bool
    remaining: int
    total: int


class SourceDiscoveryState:
    """Fence search caching and the cumulative probe budget to one active job lease."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        lease: JobLease,
        *,
        request_id: str,
        request_track_id: str,
        policy_fingerprint: str,
        max_diagnostic_runs: int = 3,
    ) -> None:
        self._factory = factory
        self._lease = lease
        self._request_id = request_id
        self._request_track_id = request_track_id
        self._policy_fingerprint = policy_fingerprint
        self._max_diagnostic_runs = min(MAX_DIAGNOSTIC_RUNS, max(1, max_diagnostic_runs))

    def cached_search(
        self,
        provider: ProviderIdentity,
        query: str,
        *,
        maximum_candidates: int,
    ) -> CachedSourceSearch | None:
        key = self._search_cache_key(provider, query)
        with self._factory.begin() as session:
            self._leased_job(session, action="reading source-search cache")
            entry = ExternalCacheRepository(session).get(SOURCE_SEARCH_CACHE_NAMESPACE, key)
        if entry is None or not isinstance(entry.payload, dict):
            return None
        payload = entry.payload
        if (
            payload.get("schema_version") != SOURCE_SEARCH_CACHE_SCHEMA_VERSION
            or payload.get("provider") != provider.value
        ):
            return None
        found_count = _bounded_count(payload.get("found_count"), maximum=maximum_candidates)
        invalid_count = _bounded_count(payload.get("invalid_count"), maximum=maximum_candidates)
        raw_candidates = payload.get("candidates")
        if found_count is None or invalid_count is None or not isinstance(raw_candidates, list):
            return None
        if len(raw_candidates) > maximum_candidates:
            return None
        candidates: list[SourceCandidate] = []
        for raw in raw_candidates:
            try:
                candidate = SourceCandidate.model_validate(raw)
            except ValueError:
                return None
            if (
                candidate.provider is not provider
                or candidate.extractor != provider_capability(provider).canonical_extractor
                or provider_for_url(candidate.url) is not provider
            ):
                return None
            candidates.append(candidate)
        if len(candidates) + invalid_count != found_count:
            return None
        return CachedSourceSearch(tuple(candidates), found_count, invalid_count)

    def store_search(
        self,
        provider: ProviderIdentity,
        query: str,
        result: CachedSourceSearch,
    ) -> None:
        key = self._search_cache_key(provider, query)
        payload = {
            "schema_version": SOURCE_SEARCH_CACHE_SCHEMA_VERSION,
            "provider": provider.value,
            "found_count": result.found_count,
            "invalid_count": result.invalid_count,
            "candidates": [candidate.model_dump(mode="json") for candidate in result.candidates],
        }
        with self._factory.begin() as session:
            self._leased_job(session, action="writing source-search cache")
            ExternalCacheRepository(session).put(
                SOURCE_SEARCH_CACHE_NAMESPACE,
                key,
                payload,
                ttl=SOURCE_SEARCH_CACHE_TTL,
            )

    def reserve_probe(
        self,
        identity: str,
        *,
        maximum_total: int,
        epoch: str | None = None,
        maximum_epoch: int | None = None,
    ) -> ProbeReservation:
        """Reserve before the external call, so crashes cannot reset the hard budget."""

        if (epoch is None) != (maximum_epoch is None):
            raise ValueError("probe epoch and limit must be provided together")
        if epoch is not None and epoch != "provider_fallback":
            raise ValueError("unknown source-probe epoch")
        if maximum_total <= 0 or (
            maximum_epoch is not None and not 0 < maximum_epoch <= maximum_total
        ):
            raise ValueError("source-probe limits must be positive and bounded")
        identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self._factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._leased_job(session, action="reserving a source probe")
            row, payload = self._diagnostic_row(session)
            ledger = _probe_ledger(payload)
            total = ledger["total"]
            counts = ledger["counts"]
            epochs = ledger["epochs"]
            prior = counts.get(identity_hash, 0)
            epoch_total = epochs.get(epoch, 0) if epoch is not None else 0
            epoch_exhausted = maximum_epoch is not None and epoch_total >= maximum_epoch
            if (
                total >= maximum_total
                or prior >= MAX_PROBE_ATTEMPTS_PER_IDENTITY
                or epoch_exhausted
            ):
                session.commit()
                return ProbeReservation(
                    False,
                    _remaining_probe_capacity(
                        total,
                        maximum_total=maximum_total,
                        epoch_total=epoch_total,
                        maximum_epoch=maximum_epoch,
                    ),
                    total,
                )
            counts[identity_hash] = prior + 1
            total += 1
            if epoch is not None:
                epoch_total += 1
                epochs[epoch] = epoch_total
            payload["_probe_ledger"] = {
                "total": total,
                "counts": counts,
                "epochs": epochs,
            }
            row.sanitized_metadata_json = _encode(payload)
            session.commit()
        return ProbeReservation(
            True,
            _remaining_probe_capacity(
                total,
                maximum_total=maximum_total,
                epoch_total=epoch_total,
                maximum_epoch=maximum_epoch,
            ),
            total,
        )

    def probe_total(self) -> int:
        with self._factory.begin() as session:
            self._leased_job(session, action="reading the source-probe budget")
            _row, payload = self._diagnostic_row(session)
            return _probe_ledger(payload)["total"]

    def probe_attempt_counts(self, identities: list[str]) -> dict[str, int]:
        """Return durable attempt counts without exposing hashed ledger identities."""

        if not identities:
            return {}
        with self._factory.begin() as session:
            self._leased_job(session, action="reading source-probe attempts")
            _row, payload = self._diagnostic_row(session)
            counts = _probe_ledger(payload)["counts"]
        return {
            identity: counts.get(hashlib.sha256(identity.encode("utf-8")).hexdigest(), 0)
            for identity in identities
        }

    def persist_diagnostics(self, diagnostics: dict[str, object]) -> None:
        """Store one non-executable diagnostic envelope even when no candidate survived."""

        bounded = _bounded_diagnostics(diagnostics)
        with self._factory() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            self._leased_job(session, action="persisting source-search diagnostics")
            row, payload = self._diagnostic_row(session)
            previous = payload.get("discovery_diagnostics")
            has_new_work = _diagnostics_have_work(bounded)
            if not (
                isinstance(previous, dict) and _diagnostics_have_work(previous) and not has_new_work
            ):
                payload["discovery_diagnostics"] = bounded
            if has_new_work:
                runs = _diagnostic_runs(payload)
                if not runs and isinstance(previous, dict) and _diagnostics_have_work(previous):
                    runs.append(_bounded_diagnostics(previous))
                if not runs or runs[-1] != bounded:
                    runs.append(bounded)
                payload["discovery_diagnostic_runs"] = runs[-self._max_diagnostic_runs :]
            row.sanitized_metadata_json = _encode(payload)
            session.commit()

    def _search_cache_key(self, provider: ProviderIdentity, query: str) -> str:
        normalized_query = " ".join(unicodedata.normalize("NFKC", query).casefold().split())
        material = json.dumps(
            {
                "schema_version": SOURCE_SEARCH_CACHE_SCHEMA_VERSION,
                "job_id": self._lease.job_id,
                "provider": provider.value,
                "query": normalized_query,
                "policy_fingerprint": self._policy_fingerprint,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _diagnostic_row(self, session: Session) -> tuple[EvidenceReference, dict[str, object]]:
        row = session.scalar(
            select(EvidenceReference)
            .where(
                EvidenceReference.job_id == self._lease.job_id,
                EvidenceReference.request_track_id == self._request_track_id,
                EvidenceReference.evidence_kind == SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
            )
            .order_by(EvidenceReference.created_at, EvidenceReference.id)
            .limit(1)
        )
        if row is None:
            row = EvidenceReference(
                request_id=self._request_id,
                request_track_id=self._request_track_id,
                job_id=self._lease.job_id,
                provider="automatic",
                evidence_kind=SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
                status="available",
                sanitized_metadata_json="{}",
            )
            session.add(row)
            session.flush()
            return row, {}
        try:
            parsed = json.loads(row.sanitized_metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        return row, parsed if isinstance(parsed, dict) else {}

    def _leased_job(self, session: Session, *, action: str) -> DownloadJob:
        job = session.scalar(
            select(DownloadJob).where(
                DownloadJob.id == self._lease.job_id,
                DownloadJob.lease_token == self._lease.token,
                DownloadJob.status == "active",
                DownloadJob.lease_expires_at.is_not(None),
                DownloadJob.lease_expires_at >= datetime.now(UTC),
            )
        )
        if job is None:
            raise LeaseLostError(f"job lease was lost while {action}")
        return job


def delete_expired_source_search_cache(
    factory: sessionmaker[Session], *, now: datetime | None = None
) -> int:
    """Delete only expired worker search entries, leaving shared provider caches alone."""

    with factory.begin() as session:
        return ExternalCacheRepository(session).delete_expired(
            namespace=SOURCE_SEARCH_CACHE_NAMESPACE,
            now=now,
        )


def _probe_ledger(payload: dict[str, object]) -> _ProbeLedger:
    raw = payload.get("_probe_ledger")
    if not isinstance(raw, dict):
        return {"total": 0, "counts": {}, "epochs": {}}
    total = _bounded_count(raw.get("total"), maximum=10_000) or 0
    raw_counts = raw.get("counts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for key, value in list(raw_counts.items())[:100]:
            if (
                isinstance(key, str)
                and len(key) == 64
                and all(character in "0123456789abcdef" for character in key)
                and (count := _bounded_count(value, maximum=MAX_PROBE_ATTEMPTS_PER_IDENTITY))
                is not None
                and count > 0
            ):
                counts[key] = count
    # The separately stored total is authoritative for crash-safe reservations;
    # never reduce it merely because a malformed per-identity map was repaired.
    epochs: dict[str, int] = {}
    raw_epochs = raw.get("epochs")
    if isinstance(raw_epochs, dict):
        fallback_count = _bounded_count(
            raw_epochs.get("provider_fallback"),
            maximum=MAX_RECORDED_SOURCE_DISCOVERY_PROBES,
        )
        if fallback_count is not None:
            epochs["provider_fallback"] = fallback_count
    return {"total": total, "counts": counts, "epochs": epochs}


def _remaining_probe_capacity(
    total: int,
    *,
    maximum_total: int,
    epoch_total: int,
    maximum_epoch: int | None,
) -> int:
    remaining = max(0, maximum_total - total)
    if maximum_epoch is not None:
        remaining = min(remaining, max(0, maximum_epoch - epoch_total))
    return remaining


def _bounded_count(value: object, *, maximum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
        return value
    return None


def _encode(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded_diagnostics(value: dict[str, object]) -> dict[str, object]:
    """Copy only the fixed, non-sensitive diagnostic schema into durable state."""

    result: dict[str, object] = {"schema_version": 1}
    for key, maximum in (
        ("query_variant_count", 6),
        ("found_count", 1_000),
        ("probed_count", MAX_RECORDED_SOURCE_DISCOVERY_PROBES),
        ("accepted_count", 100),
    ):
        result[key] = _bounded_count(value.get(key), maximum=maximum) or 0
    attempts: list[dict[str, object]] = []
    raw_attempts = value.get("query_attempts")
    if isinstance(raw_attempts, list):
        for raw in raw_attempts[:12]:
            if not isinstance(raw, dict):
                continue
            provider = raw.get("provider")
            query = raw.get("query")
            found = _bounded_count(raw.get("found_count"), maximum=6)
            if (
                not isinstance(provider, str)
                or provider not in _DIAGNOSTIC_PROVIDERS
                or not isinstance(query, str)
                or found is None
            ):
                continue
            normalized_query = "".join(
                character
                for character in unicodedata.normalize("NFKC", query)
                if not unicodedata.category(character).startswith("C")
            )
            normalized_query = " ".join(normalized_query.split())[:300]
            if normalized_query:
                if _DIAGNOSTIC_QUERY_SECRET.search(normalized_query):
                    normalized_query = "[redacted unsafe query]"
                attempts.append(
                    {"provider": provider, "query": normalized_query, "found_count": found}
                )
    result["query_attempts"] = attempts
    raw_rejections = value.get("rejection_counts")
    rejections: dict[str, int] = {}
    if isinstance(raw_rejections, dict):
        for key in sorted(_DIAGNOSTIC_REJECTION_CODES):
            count = _bounded_count(raw_rejections.get(key), maximum=1_000)
            if count:
                rejections[key] = count
    result["rejection_counts"] = rejections
    result["stopped_early"] = value.get("stopped_early") is True
    return result


def _diagnostics_have_work(value: dict[str, object]) -> bool:
    for key in ("found_count", "probed_count", "accepted_count"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return True
    attempts = value.get("query_attempts")
    if isinstance(attempts, list) and attempts:
        return True
    rejections = value.get("rejection_counts")
    return isinstance(rejections, dict) and bool(rejections)


def _diagnostic_runs(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_runs = payload.get("discovery_diagnostic_runs")
    if not isinstance(raw_runs, list):
        return []
    return [
        _bounded_diagnostics(run)
        for run in raw_runs[-MAX_DIAGNOSTIC_RUNS:]
        if isinstance(run, dict) and _diagnostics_have_work(run)
    ]
