from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    SourceCandidate,
)
from app.sources import EXECUTABLE_EVIDENCE_KINDS

_ACQUISITION_PROVIDERS = frozenset({"bandcamp", "soundcloud", "youtube"})


class DecisionConflict(ValueError):
    """Raised when a review submission no longer describes the pending bundle."""


@dataclass(frozen=True, slots=True)
class DecisionSelection:
    decision_id: str
    option_id: str
    correction: Mapping[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class AppliedDecisionBundle:
    job: DownloadJob
    replayed: bool


@dataclass(frozen=True, slots=True)
class CanonicalDecisionReplay:
    payload: dict[str, object]
    local_confidence: float | None
    model_confidence: float | None
    openai_call_id: str | None


_CANONICAL_SELECTION_FIELDS = frozenset(
    {
        "artist",
        "title",
        "album",
        "album_artist",
        "year",
        "duration_seconds",
        "recording_mbid",
        "release_mbid",
        "release_group_mbid",
        "version",
        "version_signature",
        "recording_candidate_id",
        "release_candidate_id",
    }
)


def stable_payload(value: object) -> object:
    """Return a bounded JSON-safe value used for deterministic fingerprints.

    Provider payloads are data, never instructions. Unsupported runtime objects and
    non-finite numbers are rejected rather than acquiring an unstable string form.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("decision payload contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("decision payload keys must be strings")
            result[key[:160]] = stable_payload(value[key])
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [stable_payload(item) for item in value]
    raise ValueError("decision payload contains an unsupported value")


def _stable_mapping(value: Mapping[str, object]) -> dict[str, object]:
    result = stable_payload(value)
    if not isinstance(result, dict):  # Defensive: mappings always normalize to dictionaries.
        raise ValueError("decision payload must be an object")
    return result


def payload_fingerprint(value: object) -> str:
    encoded = json.dumps(
        stable_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def candidate_set_fingerprint(category: str, options: Sequence[Mapping[str, object]]) -> str:
    return payload_fingerprint(
        {
            "category": category,
            "options": sorted(
                (stable_payload(option) for option in options),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            ),
        }
    )


def review_bundle_fingerprint(decisions: Sequence[JobDecision]) -> str:
    return payload_fingerprint(
        [
            {
                "decision_id": decision.id,
                "category": decision.category,
                "fingerprint": decision.candidate_set_fingerprint,
                "revision": decision.revision,
            }
            for decision in sorted(decisions, key=lambda item: (item.category, item.id))
        ]
    )


def create_pending_decision(
    session: Session,
    job: DownloadJob,
    *,
    category: str,
    reason: str,
    options: Sequence[Mapping[str, object]],
    local_confidence: float | None = None,
    model_confidence: float | None = None,
    openai_call_id: str | None = None,
    prompt_version: str | None = None,
) -> JobDecision:
    safe_options = [_stable_mapping(option) for option in options]
    fingerprint = candidate_set_fingerprint(category, safe_options)
    existing = session.scalar(
        select(JobDecision)
        .where(
            JobDecision.job_id == job.id,
            JobDecision.category == category,
            JobDecision.candidate_set_fingerprint == fingerprint,
        )
        .order_by(JobDecision.revision.desc())
        .limit(1)
    )
    if existing is not None:
        # A selected fingerprint is authoritative and must be replayed, never
        # presented again. A pending fingerprint is reused after restart.
        return existing

    for prior in session.scalars(
        select(JobDecision).where(
            JobDecision.job_id == job.id,
            JobDecision.category == category,
            JobDecision.state == "pending",
        )
    ):
        prior.state = "superseded"

    job.decision_revision += 1
    job.review_round_count += 1
    decision = JobDecision(
        job_id=job.id,
        category=category,
        candidate_set_fingerprint=fingerprint,
        revision=job.decision_revision,
        state="pending",
        local_confidence=_bounded_confidence(local_confidence),
        model_confidence=_bounded_confidence(model_confidence),
        openai_call_id=openai_call_id,
        prompt_version=prompt_version,
        contradictions_json="[]",
        reason_codes_json=json.dumps([reason[:160]], separators=(",", ":")),
        round_number=job.review_round_count,
    )
    session.add(decision)
    session.flush()

    for ordinal, option in enumerate(safe_options, start=1):
        rank_value = option.get("rank", ordinal)
        score_value = option.get("score", 0.0)
        rank = rank_value if isinstance(rank_value, int) and rank_value > 0 else ordinal
        score = (
            float(score_value)
            if isinstance(score_value, (int, float)) and not isinstance(score_value, bool)
            else 0.0
        )
        option_payload = {key: value for key, value in option.items() if key != "score"}
        option_key = _option_key(option_payload)
        option_fingerprint = payload_fingerprint(option_payload)
        session.add(
            JobReviewOption(
                job_id=job.id,
                decision_id=decision.id,
                kind=category,
                rank=rank,
                option_key=option_key,
                fingerprint=option_fingerprint,
                revision=decision.revision,
                materially_different=bool(option_payload.get("materially_different", True)),
                provider_payload_json=json.dumps(
                    option_payload, ensure_ascii=False, separators=(",", ":")
                ),
                score=max(0.0, min(1.0, score)),
            )
        )
    return decision


def record_selected_decision(
    session: Session,
    job: DownloadJob,
    *,
    category: str,
    candidates: Sequence[Mapping[str, object]],
    selected_payload: Mapping[str, object],
    decided_by: str,
    reason_codes: Sequence[str],
    local_confidence: float | None = None,
    model_confidence: float | None = None,
    openai_call_id: str | None = None,
    prompt_version: str | None = None,
    contradictions: Sequence[str] = (),
) -> JobDecision:
    if decided_by not in {"deterministic", "openai", "user", "migration"}:
        raise ValueError("unsupported decision authority")
    safe_candidates = [_stable_mapping(candidate) for candidate in candidates]
    fingerprint = candidate_set_fingerprint(category, safe_candidates)
    existing = session.scalar(
        select(JobDecision)
        .where(
            JobDecision.job_id == job.id,
            JobDecision.category == category,
            JobDecision.candidate_set_fingerprint == fingerprint,
            JobDecision.state == "selected",
        )
        .order_by(JobDecision.revision.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    for prior in session.scalars(
        select(JobDecision).where(
            JobDecision.job_id == job.id,
            JobDecision.category == category,
            JobDecision.state == "pending",
        )
    ):
        prior.state = "superseded"
    job.decision_revision += 1
    now = datetime.now(UTC)
    decision = JobDecision(
        job_id=job.id,
        category=category,
        candidate_set_fingerprint=fingerprint,
        revision=job.decision_revision,
        state="selected",
        selected_payload_json=json.dumps(
            stable_payload(selected_payload), ensure_ascii=False, separators=(",", ":")
        ),
        decided_by=decided_by,
        openai_call_id=openai_call_id,
        prompt_version=prompt_version,
        local_confidence=_bounded_confidence(local_confidence),
        model_confidence=_bounded_confidence(model_confidence),
        contradictions_json=json.dumps(list(dict.fromkeys(contradictions)), separators=(",", ":")),
        reason_codes_json=json.dumps(list(dict.fromkeys(reason_codes)), separators=(",", ":")),
        round_number=0,
        decided_at=now,
    )
    session.add(decision)
    session.flush()
    return decision


def apply_review_bundle(
    session: Session,
    job: DownloadJob,
    *,
    bundle_fingerprint: str,
    revision: int,
    selections: Sequence[DecisionSelection],
) -> AppliedDecisionBundle:
    all_decisions = list(
        session.scalars(
            select(JobDecision)
            .where(JobDecision.job_id == job.id)
            .order_by(JobDecision.category, JobDecision.id)
        )
    )
    pending = [decision for decision in all_decisions if decision.state == "pending"]
    selected = [decision for decision in all_decisions if decision.state == "selected"]

    if not pending:
        if _is_exact_replay(
            session,
            selected,
            bundle_fingerprint=bundle_fingerprint,
            revision=revision,
            selections=selections,
        ):
            return AppliedDecisionBundle(job=job, replayed=True)
        raise DecisionConflict("this review bundle has already been decided")
    if job.status != "needs_review":
        raise DecisionConflict("job is not awaiting this review bundle")
    if revision != job.decision_revision:
        raise DecisionConflict("review bundle revision is stale")
    if bundle_fingerprint != review_bundle_fingerprint(pending):
        raise DecisionConflict("review bundle fingerprint is stale")
    by_id = {selection.decision_id: selection for selection in selections}
    if set(by_id) != {decision.id for decision in pending}:
        raise DecisionConflict("every pending decision must be selected atomically")

    snapshot = _load_snapshot(job)
    now = datetime.now(UTC)
    decision_ids = sorted(decision.id for decision in pending)
    normalized_corrections = {
        decision_id: _normalize_correction(selection.correction)
        for decision_id, selection in by_id.items()
    }
    bundle_corrections = [
        correction for correction in normalized_corrections.values() if correction is not None
    ]
    if len(bundle_corrections) > 1:
        raise DecisionConflict("only one bundle-level metadata correction is allowed")
    bundle_correction = bundle_corrections[0] if bundle_corrections else None
    for decision in pending:
        selection = by_id[decision.id]
        option = session.scalar(
            select(JobReviewOption).where(
                JobReviewOption.id == selection.option_id,
                JobReviewOption.job_id == job.id,
                JobReviewOption.decision_id == decision.id,
                JobReviewOption.revision == decision.revision,
            )
        )
        if option is None:
            raise DecisionConflict("a selected option is not part of this review bundle")
        payload = _load_option(option)
        _apply_option(session, job, decision.category, payload, snapshot)
        correction = normalized_corrections[decision.id]
        authoritative_payload = dict(payload)
        if decision.category == "canonical_metadata":
            _apply_correction_values(authoritative_payload, bundle_correction)
            authoritative_payload["_applied_correction"] = bundle_correction
        option.selected_at = now
        decision.state = "selected"
        decision.selected_payload_json = json.dumps(
            {
                **authoritative_payload,
                "_review_selection": {
                    "bundle_fingerprint": bundle_fingerprint,
                    "revision": revision,
                    "decision_ids": decision_ids,
                    "option_id": option.id,
                    "correction": correction,
                    "bundle_correction": bundle_correction,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        decision.decided_by = "user"
        decision.decided_at = now

    if bundle_correction is not None:
        # A correction describes the final approved job, not one option. Apply it
        # after every selected option so category ordering cannot overwrite it.
        _apply_correction(bundle_correction, snapshot)

    if not _nonempty_string(snapshot.get("artist")) or not _nonempty_string(snapshot.get("title")):
        raise DecisionConflict("artist and title are required")
    job.approved_snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    job.status = "queued"
    job.stage = "queued"
    job.available_at = now
    job.error_code = None
    job.error_message = None
    return AppliedDecisionBundle(job=job, replayed=False)


def selected_payload(
    session: Session, job_id: str, category: str, fingerprint: str
) -> dict[str, object] | None:
    decision = selected_decision(session, job_id, category, fingerprint)
    if decision is None or decision.selected_payload_json is None:
        return None
    try:
        value = json.loads(decision.selected_payload_json)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def selected_decision(
    session: Session, job_id: str, category: str, fingerprint: str
) -> JobDecision | None:
    return session.scalar(
        select(JobDecision).where(
            JobDecision.job_id == job_id,
            JobDecision.category == category,
            JobDecision.candidate_set_fingerprint == fingerprint,
            JobDecision.state == "selected",
        )
    )


def latest_user_canonical_selection(
    session: Session, job_id: str
) -> CanonicalDecisionReplay | None:
    """Reconstruct a durable user decision without trusting copied provider fields."""

    decision = session.scalar(
        select(JobDecision)
        .where(
            JobDecision.job_id == job_id,
            JobDecision.category == "canonical_metadata",
            JobDecision.state == "selected",
            JobDecision.decided_by == "user",
        )
        .order_by(JobDecision.revision.desc(), JobDecision.id.desc())
        .limit(1)
    )
    if decision is None or decision.selected_payload_json is None:
        return None
    try:
        stored = json.loads(decision.selected_payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(stored, dict):
        return None
    review = stored.get("_review_selection")
    if not isinstance(review, dict):
        return None
    option_id = _nonempty_string(review.get("option_id"))
    decision_ids = review.get("decision_ids")
    if (
        option_id is None
        or not isinstance(decision_ids, list)
        or decision.id not in decision_ids
        or not all(isinstance(value, str) for value in decision_ids)
    ):
        return None
    option = session.scalar(
        select(JobReviewOption).where(
            JobReviewOption.id == option_id,
            JobReviewOption.job_id == job_id,
            JobReviewOption.decision_id == decision.id,
            JobReviewOption.kind == "canonical_metadata",
            JobReviewOption.revision == decision.revision,
            JobReviewOption.selected_at.is_not(None),
        )
    )
    if option is None:
        return None
    try:
        server_payload = _load_option(option)
        nested_correction = _normalize_correction(_correction_mapping(review.get("correction")))
        raw_bundle = review.get("bundle_correction", _MISSING)
        bundle_correction = (
            nested_correction
            if raw_bundle is _MISSING
            else _normalize_correction(_correction_mapping(raw_bundle))
        )
        raw_applied = stored.get("_applied_correction", _MISSING)
        applied_correction = (
            bundle_correction
            if raw_applied is _MISSING
            else _normalize_correction(_correction_mapping(raw_applied))
        )
    except DecisionConflict:
        return None
    if nested_correction is not None and nested_correction != bundle_correction:
        return None
    if raw_applied is not _MISSING and (
        raw_bundle is _MISSING or applied_correction != bundle_correction
    ):
        return None
    payload = {
        key: server_payload[key] for key in _CANONICAL_SELECTION_FIELDS if key in server_payload
    }
    _apply_correction_values(payload, applied_correction)
    if (
        _nonempty_string(payload.get("artist")) is None
        or _nonempty_string(payload.get("title")) is None
    ):
        return None
    if raw_applied is not _MISSING:
        for key in ("artist", "title", "album"):
            if stored.get(key) != payload.get(key):
                return None
    return CanonicalDecisionReplay(
        payload=payload,
        local_confidence=decision.local_confidence,
        model_confidence=decision.model_confidence,
        openai_call_id=decision.openai_call_id,
    )


def _apply_option(
    session: Session,
    job: DownloadJob,
    category: str,
    payload: Mapping[str, object],
    snapshot: dict[str, object],
) -> None:
    if category == "acquisition_source":
        if payload.get("allow_provider_fallback") is True:
            snapshot_requested, snapshot_excluded = _provider_constraints(snapshot)
            option_requested, option_excluded = _provider_constraints(payload)
            if not snapshot_requested and not snapshot_excluded:
                raise DecisionConflict("provider fallback option is invalid")
            if option_requested != snapshot_requested or option_excluded != snapshot_excluded:
                raise DecisionConflict("provider fallback no longer matches the approved request")
            fallback_providers = payload.get("fallback_providers")
            if not isinstance(fallback_providers, list) or not fallback_providers:
                raise DecisionConflict("provider fallback has no permitted alternatives")
            initial_scope = set(snapshot_requested or _ACQUISITION_PROVIDERS)
            initial_scope.difference_update(snapshot_excluded)
            if not all(
                isinstance(provider, str)
                and provider in _ACQUISITION_PROVIDERS
                and provider not in initial_scope
                for provider in fallback_providers
            ):
                raise DecisionConflict("provider fallback contains an invalid alternative")
            snapshot["provider_fallback_allowed"] = True
            snapshot["provider_fallback_providers"] = list(dict.fromkeys(fallback_providers))
            job.active_source_candidate_id = None
            return
        candidate_id = _nonempty_string(payload.get("source_candidate_id"))
        if candidate_id is None:
            raise DecisionConflict("source option has no durable candidate")
        candidate = session.scalar(
            select(SourceCandidate)
            .outerjoin(
                EvidenceReference,
                SourceCandidate.evidence_id == EvidenceReference.id,
            )
            .where(
                SourceCandidate.id == candidate_id,
                SourceCandidate.job_id == job.id,
                or_(
                    SourceCandidate.evidence_id.is_(None),
                    EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                ),
                SourceCandidate.policy_status == "allowed",
                SourceCandidate.probe_status == "valid",
            )
        )
        if candidate is None:
            raise DecisionConflict("source option is no longer safe or available")
        job.active_source_candidate_id = candidate.id
        return
    allowed = {
        "artist",
        "title",
        "album",
        "album_artist",
        "year",
        "recording_mbid",
        "release_mbid",
        "release_group_mbid",
        "version_signature",
        "duplicate_track_id",
    }
    for key in allowed:
        if key in payload:
            snapshot[key] = payload[key]
    if category == "canonical_metadata":
        if payload.get("recording_mbid"):
            snapshot["canonical_identity_verified"] = True
        raw_provenance = snapshot.get("metadata_provenance")
        provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else {}
        raw_resolution = provenance.get("canonical_metadata_resolution")
        resolution = dict(raw_resolution) if isinstance(raw_resolution, Mapping) else {}
        resolution.update(
            {
                "automatic_association": False,
                "source": "user_confirmed_server_candidate",
                "decided_by": "user",
            }
        )
        provenance["canonical_metadata_resolution"] = resolution
        snapshot["metadata_provenance"] = provenance


def _apply_correction(correction: Mapping[str, str | None], snapshot: dict[str, object]) -> None:
    unknown = set(correction) - {"artist", "title", "album"}
    if unknown:
        raise DecisionConflict("metadata correction contains unsupported fields")
    for key, value in correction.items():
        if value is None:
            if key == "album":
                snapshot[key] = None
                _set_album_constraint_explicit(snapshot, False)
            continue
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 300:
            raise DecisionConflict("metadata correction is invalid")
        snapshot[key] = cleaned
        if key == "album":
            _set_album_constraint_explicit(snapshot, True)


def _apply_correction_values(
    target: dict[str, object], correction: Mapping[str, str | None] | None
) -> None:
    if correction is None:
        return
    for key, value in correction.items():
        if value is None:
            if key == "album":
                target[key] = None
            continue
        target[key] = value


def _normalize_correction(
    correction: Mapping[str, str | None] | None,
) -> dict[str, str | None] | None:
    if correction is None:
        return None
    unknown = set(correction) - {"artist", "title", "album"}
    if unknown:
        raise DecisionConflict("metadata correction contains unsupported fields")
    normalized: dict[str, str | None] = {}
    for key in sorted(correction):
        value = correction[key]
        if value is None:
            normalized[key] = None
            continue
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 300:
            raise DecisionConflict("metadata correction is invalid")
        normalized[key] = cleaned
    return normalized


_MISSING = object()


def _correction_mapping(value: object) -> Mapping[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DecisionConflict("metadata correction is invalid")
    if not all(
        isinstance(key, str) and (item is None or isinstance(item, str))
        for key, item in value.items()
    ):
        raise DecisionConflict("metadata correction is invalid")
    return value


def _set_album_constraint_explicit(snapshot: dict[str, object], explicit: bool) -> None:
    value = snapshot.get("metadata_provenance")
    provenance = dict(value) if isinstance(value, Mapping) else {}
    raw_user_constraints = provenance.get("user_constraints")
    user_constraints = (
        dict(raw_user_constraints) if isinstance(raw_user_constraints, Mapping) else {}
    )
    user_constraints["album_constraint_explicit"] = explicit
    user_constraints["requested_album"] = snapshot.get("album") if explicit else None
    provenance["user_constraints"] = user_constraints
    snapshot["metadata_provenance"] = provenance


def _is_exact_replay(
    session: Session,
    selected_decisions: Sequence[JobDecision],
    *,
    bundle_fingerprint: str,
    revision: int,
    selections: Sequence[DecisionSelection],
) -> bool:
    if not selections:
        return False
    by_id = {selection.decision_id: selection for selection in selections}
    if len(by_id) != len(selections):
        return False
    selected_by_id = {decision.id: decision for decision in selected_decisions}
    if not set(by_id).issubset(selected_by_id):
        return False
    expected_ids: set[str] | None = None
    for decision_id, selection in by_id.items():
        decision = selected_by_id[decision_id]
        option = session.get(JobReviewOption, selection.option_id)
        try:
            selected_payload = json.loads(decision.selected_payload_json or "null")
        except json.JSONDecodeError:
            return False
        if not isinstance(selected_payload, dict):
            return False
        review = selected_payload.get("_review_selection")
        if not isinstance(review, dict):
            # Legacy selections did not preserve enough information to prove an
            # exact HTTP replay and therefore fail closed.
            return False
        raw_decision_ids = review.get("decision_ids")
        if not isinstance(raw_decision_ids, list) or not all(
            isinstance(value, str) for value in raw_decision_ids
        ):
            return False
        review_ids = set(raw_decision_ids)
        if expected_ids is None:
            expected_ids = review_ids
        if (
            review_ids != expected_ids
            or review.get("bundle_fingerprint") != bundle_fingerprint
            or review.get("revision") != revision
            or review.get("option_id") != selection.option_id
            or review.get("correction") != _normalize_correction(selection.correction)
        ):
            return False
        if option is None or option.decision_id != decision.id or option.selected_at is None:
            return False
    return expected_ids == set(by_id)


def _load_snapshot(job: DownloadJob) -> dict[str, object]:
    try:
        value = json.loads(job.approved_snapshot_json)
    except json.JSONDecodeError as exc:
        raise DecisionConflict("approved job snapshot is invalid") from exc
    if not isinstance(value, dict):
        raise DecisionConflict("approved job snapshot is invalid")
    return value


def _load_option(option: JobReviewOption) -> dict[str, object]:
    try:
        value = json.loads(option.provider_payload_json)
    except json.JSONDecodeError as exc:
        raise DecisionConflict("review option is invalid") from exc
    if not isinstance(value, dict):
        raise DecisionConflict("review option is invalid")
    return value


def _option_key(payload: Mapping[str, object]) -> str:
    for key in (
        "source_candidate_id",
        "recording_candidate_id",
        "release_candidate_id",
        "track_id",
        "source_id",
    ):
        value = _nonempty_string(payload.get(key))
        if value is not None:
            return f"{key}:{value}"[:160]
    return f"sha256:{payload_fingerprint(payload)[:96]}"


def _nonempty_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _provider_constraints(value: Mapping[str, object]) -> tuple[frozenset[str], frozenset[str]]:
    requested = _provider_value_list(value.get("requested_providers"))
    legacy_requested = _nonempty_string(value.get("requested_provider"))
    if legacy_requested in _ACQUISITION_PROVIDERS:
        requested.add(legacy_requested)
    excluded = _provider_value_list(value.get("excluded_providers"))
    requested.difference_update(excluded)
    return frozenset(requested), frozenset(excluded)


def _provider_value_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item in _ACQUISITION_PROVIDERS}


def _bounded_confidence(value: float | None) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value):
        raise ValueError("decision confidence must be finite")
    return min(1.0, max(0.0, float(value)))
