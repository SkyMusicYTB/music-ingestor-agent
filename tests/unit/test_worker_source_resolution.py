from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import DownloadTimedOut
from app.config import Settings
from app.db.models import (
    Conversation,
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    ServiceTask,
    SourceCandidate,
    User,
)
from app.repositories.decisions import (
    DecisionSelection,
    apply_review_bundle,
    review_bundle_fingerprint,
)
from app.sources import (
    ProviderIdentity,
    UploaderRelationship,
)
from app.sources import SourceCandidate as DomainSourceCandidate
from app.workers.queue import DownloadJobQueue, JobLease, LeaseLostError
from app.workers.source_resolution import (
    SourceResolutionNeedsReview,
    WorkerSourceResolver,
    _candidate_from_metadata,
    _db_candidate_values,
    _domain_from_row,
    _requested_source_version,
)


class _Cancellation:
    def is_set(self) -> bool:
        return False


class _Monitor:
    def raise_if_unusable(self) -> None:
        return None


class _NoNetworkYtDlp:
    def search_provider(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"entries": []}

    def probe(self, *args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("persisted valid source must not be probed again")


class _TransientYtDlp(_NoNetworkYtDlp):
    def search_provider(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise DownloadTimedOut("provider search timed out")


class _EvidenceReplayYtDlp(_NoNetworkYtDlp):
    def __init__(self) -> None:
        self.probed: list[str] = []

    def probe(self, value: str, *args: object, **kwargs: object) -> dict[str, object]:
        self.probed.append(value)
        return {
            "id": "yellow-bandcamp-evidence",
            "title": "Coldplay - Yellow",
            "artist": "Coldplay",
            "uploader": "Coldplay",
            "duration": 266.0,
            "webpage_url": value,
        }


def _job(session_factory: sessionmaker[Session], suffix: str) -> tuple[str, str, str, JobLease]:
    with session_factory.begin() as session:
        user = User(
            username=f"source-{suffix}",
            username_normalized=f"source-{suffix}",
            password_hash="fixture",  # noqa: S106
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="source")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add Yellow by Coldplay",
            action="add",
            idempotency_key=f"source-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Coldplay",
            title="Yellow",
            duration_seconds=266.0,
            version_signature="studio",
            selected=True,
        )
        session.add(track)
        session.flush()
        snapshot = {
            "request_track_id": track.id,
            "artist": "Coldplay",
            "title": "Yellow",
            "duration_seconds": 266.0,
            "version_signature": "studio",
        }
        job = DownloadJob(
            request_track_id=track.id,
            approved_snapshot_json=json.dumps(snapshot),
            dedup_key=f"source:{suffix}",
            status="active",
            stage="resolving_source",
            lease_token=f"lease-{suffix}",
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        return (
            request.id,
            track.id,
            job.id,
            JobLease(
                job_id=job.id,
                token=f"lease-{suffix}",
                approved_snapshot=snapshot,
                retry_count=0,
            ),
        )


def _candidate(
    *,
    source_id: str,
    provider: str,
    extractor: str,
    url: str,
    request_track_id: str | None,
    evidence_id: str | None = None,
) -> SourceCandidate:
    return SourceCandidate(
        evidence_id=evidence_id,
        request_track_id=request_track_id,
        provider=provider,
        extractor=extractor,
        source_id=source_id,
        acquisition_url=url,
        provider_title="Coldplay - Yellow",
        provider_artist="Coldplay",
        uploader="Unrelated Fan Archive",
        uploader_relationship="third_party",
        duration_seconds=266.0,
        version_signature="studio",
        group_key="coldplay:yellow:studio",
        local_score=0.0,
        policy_status="allowed",
        probe_status="valid",
        sanitized_metadata_json=json.dumps(
            {"uploader": "Unrelated Fan Archive"}, separators=(",", ":")
        ),
    )


def _resolver(settings: Settings, session_factory: sessionmaker[Session]) -> WorkerSourceResolver:
    hardened = settings.model_copy(update={"ai_match_resolution_enabled": False})
    return WorkerSourceResolver(
        hardened,
        session_factory,
        DownloadJobQueue(session_factory),
        _NoNetworkYtDlp(),  # type: ignore[arg-type]
    )


def test_source_version_uses_explicit_constraint_then_operator_default() -> None:
    assert (
        _requested_source_version(
            {"requested_version": "live", "version_signature": "remix"},
            "studio",
        )
        == "live"
    )
    assert _requested_source_version({"version_signature": "live"}, "studio") == "studio"


def test_candidate_metadata_keeps_third_party_creator_as_uploader_provenance() -> None:
    candidate = _candidate_from_metadata(
        ProviderIdentity.YOUTUBE,
        {
            "id": "third-party-yellow",
            "title": "Coldplay - Yellow",
            "creator": "Unrelated Fan Archive",
            "uploader": "Unrelated Fan Archive",
            "duration": 266.0,
        },
    )

    assert candidate is not None
    assert candidate.artist == "Coldplay"
    assert candidate.track == "Yellow"
    assert candidate.uploader_name == "Unrelated Fan Archive"
    assert candidate.uploader_relationship is UploaderRelationship.THIRD_PARTY


def test_request_level_evidence_is_adopted_and_third_party_uploader_auto_matches(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, job_id, lease = _job(session_factory, "adopt")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=request_id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=yellow-source",
            provider_item_id="yellow-source",
            status="available",
            sanitized_metadata_json="{}",
        )
        session.add(evidence)
        session.flush()
        domain_candidate = _candidate_from_metadata(
            ProviderIdentity.YOUTUBE,
            {
                "id": "yellow-source",
                "title": "Coldplay - Yellow",
                "creator": "Unrelated Fan Archive",
                "uploader": "Unrelated Fan Archive",
                "duration": 266.0,
            },
        )
        assert domain_candidate is not None
        candidate = SourceCandidate(
            evidence_id=evidence.id,
            request_track_id=None,
            **_db_candidate_values(domain_candidate),
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    selected = _resolver(settings, session_factory).resolve(lease, _Monitor(), _Cancellation())
    assert selected.candidate_id == candidate_id
    with session_factory() as session:
        persisted_evidence = session.scalar(
            select(EvidenceReference).where(EvidenceReference.request_id == request_id)
        )
        persisted_candidate = session.get(SourceCandidate, candidate_id)
        job = session.get(DownloadJob, job_id)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "acquisition_source",
            )
        )
        assert persisted_evidence is not None and persisted_evidence.request_track_id == track_id
        assert persisted_candidate is not None
        assert persisted_candidate.request_track_id == track_id
        assert persisted_candidate.job_id == job_id
        assert persisted_candidate.uploader == "Unrelated Fan Archive"
        assert persisted_candidate.provider_artist == "Coldplay"
        assert job is not None and job.active_source_candidate_id == candidate_id
        assert decision is not None and decision.decided_by == "deterministic"
        assert session.scalar(select(ServiceTask.id)) is None


def test_source_failure_advances_once_and_restart_reuses_persisted_selection(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(session_factory, "fallback")
    with session_factory.begin() as session:
        first = _candidate(
            source_id="yellow-bandcamp",
            provider="bandcamp",
            extractor="bandcamp",
            url="https://coldplay.bandcamp.com/track/yellow",
            request_track_id=track_id,
        )
        second = _candidate(
            source_id="yellow-youtube",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-youtube",
            request_track_id=track_id,
        )
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id

    resolver = _resolver(settings, session_factory)
    selected = resolver.resolve(lease, _Monitor(), _Cancellation())
    assert selected.candidate_id == first_id
    assert resolver.reject_active(lease, "download_failed") == 1
    selected = resolver.resolve(lease, _Monitor(), _Cancellation())
    assert selected.candidate_id == second_id

    restarted = _resolver(settings, session_factory)
    replayed = restarted.resolve(lease, _Monitor(), _Cancellation())
    assert replayed.candidate_id == second_id
    with session_factory() as session:
        exhausted = session.get(SourceCandidate, first_id)
        decisions = list(
            session.scalars(
                select(JobDecision)
                .where(
                    JobDecision.job_id == job_id,
                    JobDecision.category == "acquisition_source",
                )
                .order_by(JobDecision.revision)
            )
        )
        assert exhausted is not None and exhausted.policy_status == "exhausted"
        assert [decision.state for decision in decisions] == ["rejected", "selected"]
        assert "source_failed:download_failed" in json.loads(decisions[0].reason_codes_json)


def test_exhausted_evidence_source_is_not_rediscovered_before_safe_fallback(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, _job_id, lease = _job(session_factory, "evidence-fallback")
    with session_factory.begin() as session:
        evidence = EvidenceReference(
            request_id=request_id,
            request_track_id=track_id,
            job_id=lease.job_id,
            provider="bandcamp",
            evidence_kind="direct_user_url",
            canonical_url="https://coldplay.bandcamp.com/track/yellow-evidence",
            provider_item_id="yellow-bandcamp-evidence",
            status="available",
        )
        session.add(evidence)
        session.flush()
        first = _candidate(
            source_id="yellow-bandcamp-evidence",
            provider="bandcamp",
            extractor="bandcamp",
            url="https://coldplay.bandcamp.com/track/yellow-evidence",
            request_track_id=track_id,
            evidence_id=evidence.id,
        )
        second = _candidate(
            source_id="yellow-youtube-fallback",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-youtube-fallback",
            request_track_id=track_id,
        )
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id

    ytdlp = _EvidenceReplayYtDlp()
    hardened = settings.model_copy(update={"ai_match_resolution_enabled": False})
    resolver = WorkerSourceResolver(
        hardened,
        session_factory,
        DownloadJobQueue(session_factory),
        ytdlp,  # type: ignore[arg-type]
    )

    assert resolver.resolve(lease, _Monitor(), _Cancellation()).candidate_id == first_id
    assert resolver.reject_active(lease, "download_failed") == 1
    assert resolver.resolve(lease, _Monitor(), _Cancellation()).candidate_id == second_id
    assert ytdlp.probed == []


def test_explicit_provider_filters_other_candidates_until_user_allows_fallback(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(session_factory, "provider-consent")
    constrained_snapshot = {
        **lease.approved_snapshot,
        "requested_provider": "soundcloud",
        "provider_fallback_allowed": False,
    }
    constrained_lease = JobLease(
        job_id=lease.job_id,
        token=lease.token,
        approved_snapshot=constrained_snapshot,
        retry_count=lease.retry_count,
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.approved_snapshot_json = json.dumps(constrained_snapshot)
        youtube = _candidate(
            source_id="yellow-youtube-only",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-youtube-only",
            request_track_id=track_id,
        )
        youtube.job_id = job_id
        session.add(youtube)
        session.flush()
        youtube_id = youtube.id
        job.active_source_candidate_id = youtube_id

    resolver = _resolver(settings, session_factory)
    with pytest.raises(SourceResolutionNeedsReview) as raised:
        resolver.resolve(constrained_lease, _Monitor(), _Cancellation())
    assert "explicitly requested soundcloud" in raised.value.reason
    assert raised.value.options == [
        {
            "kind": "acquisition_source",
            "rank": 1,
            "allow_provider_fallback": True,
            "requested_provider": "soundcloud",
            "requested_providers": ["soundcloud"],
            "excluded_providers": [],
            "fallback_providers": ["bandcamp", "youtube"],
            "provider": "automatic",
            "title": "Use another permitted provider",
            "score": 1.0,
            "materially_different": True,
        }
    ]

    queue = DownloadJobQueue(session_factory)
    assert queue.require_review(
        constrained_lease,
        reason=raised.value.reason,
        options=raised.value.options,
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        [decision] = list(
            session.scalars(
                select(JobDecision).where(
                    JobDecision.job_id == job_id,
                    JobDecision.state == "pending",
                )
            )
        )
        [option] = list(
            session.scalars(
                select(JobReviewOption).where(JobReviewOption.decision_id == decision.id)
            )
        )
        apply_review_bundle(
            session,
            job,
            bundle_fingerprint=review_bundle_fingerprint([decision]),
            revision=job.decision_revision,
            selections=[DecisionSelection(decision_id=decision.id, option_id=option.id)],
        )
        resumed_snapshot = json.loads(job.approved_snapshot_json)
        assert resumed_snapshot["provider_fallback_allowed"] is True
        job.status = "active"
        job.stage = "resolving_source"
        job.lease_token = "provider-consent-resume"  # noqa: S105 - inert lease fixture
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    resumed_lease = JobLease(
        job_id=job_id,
        token="provider-consent-resume",  # noqa: S106
        approved_snapshot=resumed_snapshot,
        retry_count=0,
    )
    selected = resolver.resolve(resumed_lease, _Monitor(), _Cancellation())
    assert selected.candidate_id == youtube_id


def test_stale_lease_cannot_reject_or_advance_active_source(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(session_factory, "stale-reject")
    with session_factory.begin() as session:
        candidate = _candidate(
            source_id="yellow-safe",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-safe",
            request_track_id=track_id,
        )
        candidate.job_id = job_id
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.active_source_candidate_id = candidate_id

    stale = JobLease(
        job_id=lease.job_id,
        token="stale-token",  # noqa: S106 - inert fencing-token fixture
        approved_snapshot=lease.approved_snapshot,
        retry_count=lease.retry_count,
    )
    with pytest.raises(LeaseLostError):
        _resolver(settings, session_factory).reject_active(stale, "download_failed")
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.lease_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(LeaseLostError):
        _resolver(settings, session_factory).reject_active(lease, "download_failed")

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        candidate = session.get(SourceCandidate, candidate_id)
        assert job is not None and job.active_source_candidate_id == candidate_id
        assert job.source_attempt_count == 0
        assert candidate is not None and candidate.policy_status == "allowed"
        assert candidate.failure_code is None


def test_candidate_restore_excludes_sibling_and_model_evidence_but_allows_unbound(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, _job_id, lease = _job(session_factory, "scope")
    with session_factory.begin() as session:
        sibling = RequestTrack(
            request_id=request_id,
            ordinal=2,
            artist="Radiohead",
            title="Creep",
            duration_seconds=238.0,
            version_signature="studio",
            selected=True,
        )
        session.add(sibling)
        session.flush()
        sibling_evidence = EvidenceReference(
            request_id=request_id,
            request_track_id=sibling.id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=sibling-source",
            provider_item_id="sibling-source",
            status="available",
        )
        unbound_evidence = EvidenceReference(
            request_id=request_id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=unbound-source",
            provider_item_id="unbound-source",
            status="available",
        )
        model_evidence = EvidenceReference(
            request_id=request_id,
            request_track_id=track_id,
            provider="youtube",
            evidence_kind="model_evidence",
            canonical_url="https://www.youtube.com/watch?v=model-source",
            provider_item_id="model-source",
            status="available",
        )
        session.add_all([sibling_evidence, unbound_evidence, model_evidence])
        session.flush()
        session.add_all(
            [
                _candidate(
                    source_id="sibling-source",
                    provider="youtube",
                    extractor="youtube",
                    url="https://www.youtube.com/watch?v=sibling-source",
                    request_track_id=sibling.id,
                    evidence_id=sibling_evidence.id,
                ),
                _candidate(
                    source_id="unbound-source",
                    provider="youtube",
                    extractor="youtube",
                    url="https://www.youtube.com/watch?v=unbound-source",
                    request_track_id=None,
                    evidence_id=unbound_evidence.id,
                ),
                _candidate(
                    source_id="model-source",
                    provider="youtube",
                    extractor="youtube",
                    url="https://www.youtube.com/watch?v=model-source",
                    request_track_id=track_id,
                    evidence_id=model_evidence.id,
                ),
            ]
        )

    restored = _resolver(settings, session_factory)._existing_domain_candidates(
        request_id, track_id, lease
    )
    assert [candidate.source_id for candidate in restored] == ["unbound-source"]


def test_persisted_candidate_round_trip_preserves_every_ranking_fact(
    session_factory: sessionmaker[Session],
) -> None:
    _request_id, track_id, _job_id, _lease = _job(session_factory, "ranking-facts")
    domain = DomainSourceCandidate(
        source_id="ranking-source",
        provider=ProviderIdentity.YOUTUBE,
        extractor="youtube",
        url="https://www.youtube.com/watch?v=ranking-source",
        title="Coldplay - Yellow (Live)",
        artist="Coldplay",
        track="Yellow",
        version="live",
        duration_seconds=266.0,
        uploader_name="Coldplay",
        uploader_id="trusted-channel-id",
        uploader_relationship=UploaderRelationship.OFFICIAL_ARTIST,
        audio_available=False,
        audio_quality=0.0,
        description="untrusted display text",
    )
    with session_factory.begin() as session:
        row = SourceCandidate(request_track_id=track_id, **_db_candidate_values(domain))
        session.add(row)
        session.flush()
        row_id = row.id
    with session_factory() as session:
        row = session.get(SourceCandidate, row_id)
        assert row is not None
        restored = _domain_from_row(row)

    assert restored.track == domain.track
    assert restored.version == domain.version
    assert restored.uploader_id == domain.uploader_id
    assert restored.audio_available is False
    assert restored.audio_quality == 0.0
    assert restored.description == domain.description


@pytest.mark.parametrize(
    "policy_metadata",
    [
        {"is_drm": True, "availability": "public"},
        {"is_drm": False, "availability": "premium_only"},
        {"is_drm": False, "availability": "unlisted"},
    ],
)
def test_source_candidate_parser_fails_closed_for_non_public_media(
    policy_metadata: dict[str, object],
) -> None:
    assert (
        _candidate_from_metadata(
            ProviderIdentity.YOUTUBE,
            {
                "id": "blocked-source",
                "title": "Coldplay - Yellow",
                "extractor": "youtube",
                **policy_metadata,
            },
        )
        is None
    )


def test_transient_discovery_failure_retries_job_without_consuming_source_attempt(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(session_factory, "transient-discovery")
    resolver = WorkerSourceResolver(
        settings.model_copy(update={"ai_match_resolution_enabled": False}),
        session_factory,
        DownloadJobQueue(session_factory),
        _TransientYtDlp(),  # type: ignore[arg-type]
    )

    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and job.source_attempt_count == 0
        assert job.active_source_candidate_id is None
