from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.sources.identities import UploaderRelationship, is_safe_candidate_id
from app.sources.models import FiniteScore, SourceCandidate, SourceIntent, SourceModel, SourcePolicy
from app.sources.policy import validate_source_candidate
from app.sources.ranking import RankedSource, group_ranked_sources, rank_sources
from app.sources.resolver import FiniteSourceResolver, UnknownSourceCandidate

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class MatchDecision(StrEnum):
    MATCH = "match"
    AMBIGUOUS = "ambiguous"
    REJECT = "reject"


class CanonicalMatchDecision(SourceModel):
    selected_recording_candidate_id: str | None = Field(default=None, max_length=200)
    selected_release_candidate_id: str | None = Field(default=None, max_length=200)
    recording_version: str = Field(min_length=1, max_length=100)
    decision: MatchDecision
    confidence: FiniteScore
    contradiction_codes: tuple[str, ...] = Field(max_length=16)
    reason_code: str = Field(min_length=1, max_length=64, pattern=_CODE_RE.pattern)

    @field_validator("selected_recording_candidate_id", "selected_release_candidate_id")
    @classmethod
    def safe_selected_ids(cls, value: str | None) -> str | None:
        if value is not None and not is_safe_candidate_id(value):
            raise ValueError("selected candidate ID is not safe")
        return value

    @field_validator("contradiction_codes")
    @classmethod
    def safe_contradictions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_codes(values)

    @model_validator(mode="after")
    def consistent(self) -> CanonicalMatchDecision:
        selected = self.selected_recording_candidate_id is not None
        if self.decision is MatchDecision.MATCH and not selected:
            raise ValueError("a canonical match requires a recording candidate")
        if self.decision is not MatchDecision.MATCH and (
            selected or self.selected_release_candidate_id is not None
        ):
            raise ValueError("only a canonical match may select candidates")
        if self.selected_release_candidate_id is not None and not selected:
            raise ValueError("a release candidate requires a recording candidate")
        if self.decision is MatchDecision.MATCH and self.contradiction_codes:
            raise ValueError("a canonical match cannot retain contradictions")
        return self

    @property
    def selected_candidate_id(self) -> str | None:
        return self.selected_recording_candidate_id


class SourceMatchDecision(SourceModel):
    selected_source_candidate_id: str | None = Field(default=None, max_length=200)
    decision: MatchDecision
    confidence: FiniteScore
    version_match: bool
    uploader_relationship: UploaderRelationship
    contradiction_codes: tuple[str, ...] = Field(max_length=16)
    reason_code: str = Field(min_length=1, max_length=64, pattern=_CODE_RE.pattern)

    @field_validator("selected_source_candidate_id")
    @classmethod
    def safe_selected_id(cls, value: str | None) -> str | None:
        if value is not None and not is_safe_candidate_id(value):
            raise ValueError("selected source candidate ID is not safe")
        return value

    @field_validator("contradiction_codes")
    @classmethod
    def safe_contradictions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _validated_codes(values)

    @model_validator(mode="after")
    def consistent(self) -> SourceMatchDecision:
        selected = self.selected_source_candidate_id is not None
        if self.decision is MatchDecision.MATCH and not selected:
            raise ValueError("a source match requires a selected candidate")
        if self.decision is not MatchDecision.MATCH and selected:
            raise ValueError("only a source match may select a candidate")
        if self.decision is MatchDecision.MATCH and not self.version_match:
            raise ValueError("a source match requires a compatible version")
        if self.decision is MatchDecision.MATCH and self.contradiction_codes:
            raise ValueError("a source match cannot retain contradictions")
        return self

    @property
    def selected_source_id(self) -> str | None:
        return self.selected_source_candidate_id


def validate_canonical_match_decision(
    value: CanonicalMatchDecision | Mapping[str, Any],
    *,
    recording_candidate_ids: Iterable[str],
    release_candidate_ids: Iterable[str] = (),
) -> CanonicalMatchDecision:
    recording_ids = finite_candidate_ids(recording_candidate_ids)
    release_ids = finite_candidate_ids(release_candidate_ids, allow_empty=True)
    decision = (
        value
        if isinstance(value, CanonicalMatchDecision)
        else CanonicalMatchDecision.model_validate(value)
    )
    _require_member(
        decision.selected_recording_candidate_id,
        recording_ids,
        kind="recording",
    )
    _require_member(decision.selected_release_candidate_id, release_ids, kind="release")
    return decision


def validate_source_match_decision(
    value: SourceMatchDecision | Mapping[str, Any],
    *,
    source_candidate_ids: Iterable[str],
) -> SourceMatchDecision:
    candidate_ids = finite_candidate_ids(source_candidate_ids)
    decision = (
        value
        if isinstance(value, SourceMatchDecision)
        else SourceMatchDecision.model_validate(value)
    )
    _require_member(decision.selected_source_candidate_id, candidate_ids, kind="source")
    return decision


def finite_candidate_ids(
    values: Iterable[str],
    *,
    max_candidates: int = 24,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    result = tuple(values)
    if (not result and not allow_empty) or len(result) > max_candidates:
        lower = 0 if allow_empty else 1
        raise ValueError(f"candidate ID count must be between {lower} and {max_candidates}")
    if any(not isinstance(value, str) or not is_safe_candidate_id(value) for value in result):
        raise ValueError("candidate IDs must be bounded safe identifiers")
    if len(set(result)) != len(result):
        raise ValueError("candidate IDs must be unique")
    return tuple(sorted(result))


def canonical_match_decision_schema(
    *,
    recording_candidate_ids: Iterable[str],
    release_candidate_ids: Iterable[str] = (),
) -> dict[str, Any]:
    recording_ids = finite_candidate_ids(recording_candidate_ids)
    release_ids = finite_candidate_ids(release_candidate_ids, allow_empty=True)
    return _decision_schema(
        name="canonical_match_decision",
        selected={
            "selected_recording_candidate_id": [*recording_ids, None],
            "selected_release_candidate_id": [*release_ids, None],
        },
        additional={
            "recording_version": {"type": "string", "minLength": 1, "maxLength": 100},
        },
    )


def source_match_decision_schema(*, source_candidate_ids: Iterable[str]) -> dict[str, Any]:
    candidate_ids = finite_candidate_ids(source_candidate_ids)
    return _decision_schema(
        name="source_match_decision",
        selected={"selected_source_candidate_id": [*candidate_ids, None]},
        additional={
            "version_match": {"type": "boolean"},
            "uploader_relationship": {
                "type": "string",
                "enum": [relationship.value for relationship in UploaderRelationship],
            },
        },
    )


def _decision_schema(
    *,
    name: str,
    selected: Mapping[str, list[str | None]],
    additional: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_properties = {
        field: {"type": ["string", "null"], "enum": values} for field, values in selected.items()
    }
    properties: dict[str, Any] = {
        **selected_properties,
        **additional,
        "decision": {"type": "string", "enum": [item.value for item in MatchDecision]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "contradiction_codes": {
            "type": "array",
            "items": {"type": "string", "pattern": _CODE_RE.pattern, "maxLength": 64},
            "maxItems": 16,
        },
        "reason_code": {
            "type": "string",
            "pattern": _CODE_RE.pattern,
            "minLength": 1,
            "maxLength": 64,
        },
    }
    return {
        "type": "json_schema",
        "name": name,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def decide_source_match(
    intent: SourceIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    policy: SourcePolicy | None = None,
    resolver: FiniteSourceResolver | None = None,
) -> SourceMatchDecision:
    active_policy = policy or SourcePolicy()
    if not candidates:
        return _empty_source_decision(MatchDecision.REJECT, "no_source_candidates")
    active_resolver = resolver or FiniteSourceResolver(
        candidates,
        max_candidates=active_policy.max_candidates,
    )
    ranked = _eligible_ranked(intent, candidates, active_policy)
    if not ranked:
        return _empty_source_decision(MatchDecision.REJECT, "no_eligible_source")
    groups = group_ranked_sources(ranked)
    best = groups[0].best
    if best.contradiction_codes or not best.version_match:
        return _decision_for_ranked(
            best,
            active_resolver,
            decision=MatchDecision.REJECT,
            selected=False,
            reason_code="source_contradiction",
        )
    if not best.duration_compatible:
        return _decision_for_ranked(
            best,
            active_resolver,
            decision=MatchDecision.AMBIGUOUS,
            selected=False,
            reason_code="duration_not_confirmed",
        )
    if best.score < active_policy.auto_threshold:
        return _decision_for_ranked(
            best,
            active_resolver,
            decision=MatchDecision.AMBIGUOUS,
            selected=False,
            reason_code="local_score_below_threshold",
        )
    if len(groups) > 1 and best.score - groups[1].best.score < active_policy.minimum_lead:
        return _decision_for_ranked(
            best,
            active_resolver,
            decision=MatchDecision.AMBIGUOUS,
            selected=False,
            reason_code="local_lead_too_small",
        )
    return _decision_for_ranked(
        best,
        active_resolver,
        decision=MatchDecision.MATCH,
        selected=True,
        reason_code="local_auto_match",
    )


def adjudicate_ai_source_match(
    intent: SourceIntent,
    model_decision: SourceMatchDecision | Mapping[str, Any],
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    *,
    policy: SourcePolicy | None = None,
    resolver: FiniteSourceResolver | None = None,
) -> SourceMatchDecision:
    active_policy = policy or SourcePolicy()
    active_resolver = resolver or FiniteSourceResolver(
        candidates,
        max_candidates=active_policy.max_candidates,
    )
    decision = validate_source_match_decision(
        model_decision,
        source_candidate_ids=active_resolver.candidate_ids,
    )
    if decision.decision is not MatchDecision.MATCH:
        return decision
    selected_id = decision.selected_source_candidate_id
    if selected_id is None:  # Defensive; the model validator already enforces this.
        return _empty_source_decision(MatchDecision.REJECT, "model_selection_missing")
    try:
        selected = active_resolver.resolve(selected_id)
    except UnknownSourceCandidate:
        return _empty_source_decision(MatchDecision.REJECT, "model_selection_outside_finite_set")
    ranked = _eligible_ranked(intent, candidates, active_policy)
    match = next((item for item in ranked if item.candidate == selected), None)
    if match is None:
        return _empty_source_decision(MatchDecision.REJECT, "model_selection_ineligible")

    missing_duration = intent.duration_seconds is None or selected.duration_seconds is None
    required_confidence = (
        active_policy.missing_duration_ai_confidence
        if missing_duration
        else active_policy.ai_confidence_threshold
    )
    actual_contradictions = tuple(
        sorted(set((*match.contradiction_codes, *decision.contradiction_codes)))
    )
    reason_code: str | None = None
    if decision.confidence < required_confidence:
        reason_code = "ai_confidence_below_threshold"
    elif missing_duration and not match.canonical_exact:
        reason_code = "ai_missing_duration_requires_exact_canonical"
    elif match.score < active_policy.ai_local_score_threshold:
        reason_code = "ai_local_score_below_threshold"
    elif not match.version_match:
        reason_code = "ai_version_mismatch"
    elif actual_contradictions:
        reason_code = "ai_source_contradiction"
    elif not missing_duration and not match.duration_compatible:
        reason_code = "ai_duration_mismatch"
    elif (
        decision.version_match != match.version_match
        or decision.uploader_relationship is not selected.uploader_relationship
    ):
        reason_code = "ai_recomputed_attribute_mismatch"

    if reason_code is not None:
        return SourceMatchDecision(
            selected_source_candidate_id=None,
            decision=MatchDecision.AMBIGUOUS,
            confidence=decision.confidence,
            version_match=match.version_match,
            uploader_relationship=selected.uploader_relationship,
            contradiction_codes=actual_contradictions,
            reason_code=reason_code,
        )
    return SourceMatchDecision(
        selected_source_candidate_id=selected_id,
        decision=MatchDecision.MATCH,
        confidence=decision.confidence,
        version_match=True,
        uploader_relationship=selected.uploader_relationship,
        contradiction_codes=(),
        reason_code="ai_match_accepted",
    )


def _eligible_ranked(
    intent: SourceIntent,
    candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
    policy: SourcePolicy,
) -> tuple[RankedSource, ...]:
    ranked = rank_sources(intent, candidates, policy=policy)
    return tuple(
        item
        for item in ranked
        if item.candidate.audio_available
        and all(check.allowed for check in validate_source_candidate(item.candidate, policy))
    )


def _decision_for_ranked(
    ranked: RankedSource,
    resolver: FiniteSourceResolver,
    *,
    decision: MatchDecision,
    selected: bool,
    reason_code: str,
) -> SourceMatchDecision:
    return SourceMatchDecision(
        selected_source_candidate_id=(
            resolver.candidate_id_for(ranked.candidate) if selected else None
        ),
        decision=decision,
        confidence=ranked.score,
        version_match=ranked.version_match,
        uploader_relationship=ranked.candidate.uploader_relationship,
        contradiction_codes=ranked.contradiction_codes,
        reason_code=reason_code,
    )


def _empty_source_decision(decision: MatchDecision, reason_code: str) -> SourceMatchDecision:
    return SourceMatchDecision(
        selected_source_candidate_id=None,
        decision=decision,
        confidence=0.0,
        version_match=False,
        uploader_relationship=UploaderRelationship.UNKNOWN,
        contradiction_codes=(),
        reason_code=reason_code,
    )


def _validated_codes(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ValueError("contradiction codes must be unique")
    if any(not _CODE_RE.fullmatch(value) for value in values):
        raise ValueError("contradiction code is invalid")
    return values


def _require_member(value: str | None, candidates: tuple[str, ...], *, kind: str) -> None:
    if value is not None and value not in candidates:
        raise ValueError(f"selected {kind} candidate is outside the finite set")
