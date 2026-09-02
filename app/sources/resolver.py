from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.sources.models import SourceCandidate, SourcePolicy
from app.sources.policy import validate_source_candidate
from app.sources.ranking import RankedSource, group_ranked_sources


class FiniteSourceResolutionError(ValueError):
    pass


class UnknownSourceCandidate(FiniteSourceResolutionError):
    pass


class DuplicateSourceCandidate(FiniteSourceResolutionError):
    pass


@dataclass(frozen=True, slots=True)
class SourceAttempt:
    position: int
    source_candidate_id: str
    group_id: str
    candidate: SourceCandidate
    score: float


class FiniteSourceResolver:
    """Deterministic, bounded mapping from opaque decision IDs to source identities."""

    def __init__(
        self,
        candidates: list[SourceCandidate] | tuple[SourceCandidate, ...],
        *,
        max_candidates: int = 24,
    ) -> None:
        if not 1 <= len(candidates) <= max_candidates:
            raise FiniteSourceResolutionError(
                f"source candidate count must be between 1 and {max_candidates}"
            )
        identities: dict[str, SourceCandidate] = {}
        for candidate in candidates:
            key = candidate.identity.stable_key
            if key in identities:
                raise DuplicateSourceCandidate(f"duplicate source identity: {key}")
            identities[key] = candidate

        by_id: dict[str, SourceCandidate] = {}
        id_by_identity: dict[str, str] = {}
        for identity in sorted(identities):
            candidate_id = _finite_id(identity)
            if candidate_id in by_id:
                raise DuplicateSourceCandidate("deterministic source candidate ID collision")
            by_id[candidate_id] = identities[identity]
            id_by_identity[identity] = candidate_id
        self._by_id = by_id
        self._id_by_identity = id_by_identity

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    @property
    def candidates(self) -> tuple[SourceCandidate, ...]:
        return tuple(self._by_id[candidate_id] for candidate_id in self.candidate_ids)

    def candidate_id_for(self, candidate: SourceCandidate) -> str:
        try:
            return self._id_by_identity[candidate.identity.stable_key]
        except KeyError as error:
            raise UnknownSourceCandidate("source candidate is outside the finite set") from error

    def resolve(self, source_candidate_id: str) -> SourceCandidate:
        try:
            return self._by_id[source_candidate_id]
        except KeyError as error:
            raise UnknownSourceCandidate("unknown source candidate ID") from error

    def visible_records(
        self,
        ranked: tuple[RankedSource, ...],
        *,
        limit: int = 5,
    ) -> tuple[dict[str, object], ...]:
        if limit < 1:
            return ()
        records: list[dict[str, object]] = []
        for item in ranked[:limit]:
            candidate = item.candidate
            records.append(
                {
                    "source_candidate_id": self.candidate_id_for(candidate),
                    "provider": candidate.provider.value,
                    "title": candidate.title,
                    "artist": candidate.artist,
                    "track": candidate.track,
                    "duration_seconds": candidate.duration_seconds,
                    "uploader_relationship": candidate.uploader_relationship.value,
                    "local_score": round(item.score, 6),
                }
            )
        return tuple(records)


def order_source_attempts(
    ranked: list[RankedSource] | tuple[RankedSource, ...],
    resolver: FiniteSourceResolver,
    *,
    policy: SourcePolicy | None = None,
) -> tuple[SourceAttempt, ...]:
    active_policy = policy or SourcePolicy()
    eligible = tuple(
        item
        for item in ranked
        if item.candidate.audio_available
        and all(check.allowed for check in validate_source_candidate(item.candidate, active_policy))
    )
    attempts: list[SourceAttempt] = []
    for group in group_ranked_sources(eligible):
        for item in group.ranked:
            attempts.append(
                SourceAttempt(
                    position=len(attempts) + 1,
                    source_candidate_id=resolver.candidate_id_for(item.candidate),
                    group_id=group.group_id,
                    candidate=item.candidate,
                    score=item.score,
                )
            )
            if len(attempts) == active_policy.max_attempts:
                return tuple(attempts)
    return tuple(attempts)


def _finite_id(identity: str) -> str:
    material = f"music-agent-source-v1\x1f{identity}"
    return f"src_{hashlib.sha256(material.encode()).hexdigest()[:24]}"
