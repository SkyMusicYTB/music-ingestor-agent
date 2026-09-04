from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import DownloadCancelled, DownloadTimedOut, SourceValidationError
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
    record_selected_decision,
    review_bundle_fingerprint,
)
from app.sources import (
    ProviderIdentity,
    SourceIntent,
    UploaderRelationship,
)
from app.sources import SourceCandidate as DomainSourceCandidate
from app.workers.queue import DownloadJobQueue, JobLease, LeaseLostError
from app.workers.source_discovery_state import (
    SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
    SourceDiscoveryState,
)
from app.workers.source_resolution import (
    MAX_EVIDENCE_DISCOVERY_PROBES,
    MAX_EXACT_TRACK_SEARCH_QUERIES,
    MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES,
    MAX_SEARCH_RESULTS_PER_QUERY,
    MAX_SOURCE_DISCOVERY_PROBES,
    MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK,
    SourceResolutionNeedsReview,
    WorkerSourceResolver,
    _artist_credit_search_variant,
    _candidate_from_metadata,
    _db_candidate_values,
    _domain_from_row,
    _exact_track_search_queries,
    _requested_source_version,
)


class _Cancellation:
    def is_set(self) -> bool:
        return False


class _Cancelled:
    def is_set(self) -> bool:
        return True


class _Monitor:
    def raise_if_unusable(self) -> None:
        return None


class _LostMonitor:
    def raise_if_unusable(self) -> None:
        raise LeaseLostError("fixture lost lease")


class _NoNetworkYtDlp:
    def search_provider(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"entries": []}

    def probe(self, *args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("persisted valid source must not be probed again")


class _TransientYtDlp(_NoNetworkYtDlp):
    def search_provider(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise DownloadTimedOut("provider search timed out")


class _CancelledYtDlp(_NoNetworkYtDlp):
    def search_provider(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise DownloadCancelled("provider search cancelled")


class _CascadeYtDlp:
    """Offline provider fixture with fully inspected metadata keyed by stable ID."""

    def __init__(self, searches: dict[str, list[dict[str, object]]]) -> None:
        self.searches = searches
        self.search_calls: list[tuple[str, ProviderIdentity, int]] = []
        self.probe_calls: list[str] = []
        self.full: dict[str, dict[str, object]] = {
            str(entry["id"]): dict(entry) for entries in searches.values() for entry in entries
        }

    def search_provider(
        self,
        query: str,
        *,
        provider: ProviderIdentity,
        limit: int,
        cancel_signal: object = None,
    ) -> dict[str, object]:
        del cancel_signal
        self.search_calls.append((query, provider, limit))
        return {"entries": self.searches.get(query, [])[:limit]}

    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        del cancel_signal
        self.probe_calls.append(url)
        source_id = url.rsplit("=", 1)[-1]
        return dict(self.full[source_id])


class _FailedEvidenceCascadeYtDlp(_CascadeYtDlp):
    def __init__(
        self,
        searches: dict[str, list[dict[str, object]]],
        *,
        rejected_url: str,
    ) -> None:
        super().__init__(searches)
        self.rejected_url = rejected_url

    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        if url == self.rejected_url:
            del cancel_signal
            self.probe_calls.append(url)
            raise SourceValidationError("fixture rejected evidence")
        return super().probe(url, cancel_signal=cancel_signal)


class _TransientProviderCascadeYtDlp(_CascadeYtDlp):
    def __init__(
        self,
        searches: dict[str, list[dict[str, object]]],
        *,
        transient_provider: ProviderIdentity,
    ) -> None:
        super().__init__(searches)
        self.transient_provider = transient_provider

    def search_provider(
        self,
        query: str,
        *,
        provider: ProviderIdentity,
        limit: int,
        cancel_signal: object = None,
    ) -> dict[str, object]:
        if provider is self.transient_provider:
            del cancel_signal
            self.search_calls.append((query, provider, limit))
            raise DownloadTimedOut("provider search timed out")
        return super().search_provider(
            query,
            provider=provider,
            limit=limit,
            cancel_signal=cancel_signal,
        )


class _TransientProbeCascadeYtDlp(_CascadeYtDlp):
    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        del cancel_signal
        self.probe_calls.append(url)
        raise DownloadTimedOut("provider probe timed out")


class _TransientEvidenceCascadeYtDlp(_CascadeYtDlp):
    def __init__(
        self,
        searches: dict[str, list[dict[str, object]]],
        *,
        transient_url: str,
    ) -> None:
        super().__init__(searches)
        self.transient_url = transient_url

    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        if url == self.transient_url:
            del cancel_signal
            self.probe_calls.append(url)
            raise DownloadTimedOut("evidence probe timed out")
        return super().probe(url, cancel_signal=cancel_signal)


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


class _ProviderFallbackTrancheYtDlp:
    """Exhaust one provider, then expose an exact source on the consented fallback."""

    def __init__(self) -> None:
        self.search_calls: list[tuple[str, ProviderIdentity, int]] = []
        self.probe_calls: list[str] = []
        self._soundcloud_query_indexes: dict[str, int] = {}

    def search_provider(
        self,
        query: str,
        *,
        provider: ProviderIdentity,
        limit: int,
        cancel_signal: object = None,
    ) -> dict[str, object]:
        del cancel_signal
        self.search_calls.append((query, provider, limit))
        if provider is ProviderIdentity.YOUTUBE:
            return {
                "entries": [
                    _search_metadata(
                        "youtube-fallback-exact",
                        "Gabry Ponte & KEL - Tarantella (Official Audio)",
                        "Gabry Ponte & KEL",
                    )
                ]
            }
        query_index = self._soundcloud_query_indexes.setdefault(
            query, len(self._soundcloud_query_indexes)
        )
        return {
            "entries": [
                {
                    **_search_metadata(
                        f"soundcloud-wrong-{query_index}-{entry_index}",
                        "Different Artist - Tarantella",
                        "Different Artist",
                    ),
                    "webpage_url": (
                        f"https://soundcloud.com/different-artist/wrong-{query_index}-{entry_index}"
                    ),
                }
                for entry_index in range(limit)
            ]
        }

    def probe(self, url: str, *, cancel_signal: object = None) -> dict[str, object]:
        del cancel_signal
        self.probe_calls.append(url)
        if "soundcloud.com" in url:
            raise SourceValidationError("fixture rejected the requested provider source")
        return {
            **_search_metadata(
                "youtube-fallback-exact",
                "Gabry Ponte & KEL - Tarantella (Official Audio)",
                "Gabry Ponte & KEL",
            ),
            "uploader": "Gabry Ponte & KEL",
            "webpage_url": url,
        }


def _job(
    session_factory: sessionmaker[Session],
    suffix: str,
    *,
    artist: str = "Coldplay",
    title: str = "Yellow",
    duration: float = 266.0,
    artists: tuple[str, ...] = (),
) -> tuple[str, str, str, JobLease]:
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
            raw_text=f"add {title} by {artist}",
            action="add",
            idempotency_key=f"source-{suffix}",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist=artist,
            title=title,
            duration_seconds=duration,
            version_signature="studio",
            selected=True,
        )
        session.add(track)
        session.flush()
        snapshot = {
            "request_track_id": track.id,
            "artist": artist,
            "artists": list(artists),
            "title": title,
            "duration_seconds": duration,
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
    assert (
        _requested_source_version(
            {
                "version_signature": "radio_edit",
                "metadata_provenance": {
                    "request_constraints": {"version_constraint_explicit": False},
                    "recording_version": {
                        "signature": "radio_edit",
                        "source": "provider_recording_metadata",
                    },
                },
            },
            "studio",
        )
        == "radio edit"
    )
    assert _requested_source_version({"version_signature": "live"}, "studio") == "studio"
    assert (
        _requested_source_version(
            {
                "requested_version": "live",
                "metadata_provenance": {
                    "request_constraints": {"version_constraint_explicit": False}
                },
            },
            "studio",
        )
        == "studio"
    )
    assert (
        _requested_source_version(
            {
                "requested_version": "live",
                "metadata_provenance": {
                    "request_constraints": {"version_constraint_explicit": True}
                },
            },
            "studio",
        )
        == "live"
    )


def _search_metadata(
    source_id: str,
    display_title: str,
    artist: str,
    *,
    track: str = "Tarantella",
    duration: float = 146.0,
) -> dict[str, object]:
    return {
        "id": source_id,
        "title": display_title,
        "track": track,
        "artist": artist,
        "uploader": "Unrelated Dance Archive",
        "duration": duration,
        "availability": "public",
        "acodec": "opus",
    }


def test_exact_track_query_cascade_is_unique_bounded_and_version_aware() -> None:
    studio = _exact_track_search_queries(
        SourceIntent(
            artist='Gabry Ponte, KEL "quoted"',
            artists=("Gabry Ponte", 'KEL "quoted"'),
            title="Tarantella",
            requested_version="studio",
            duration_seconds=146.0,
        )
    )
    live = _exact_track_search_queries(
        SourceIntent(
            artist="Gabry Ponte, KEL",
            artists=("Gabry Ponte", "KEL"),
            title="Tarantella",
            requested_version="live",
            duration_seconds=146.0,
        )
    )

    assert 4 <= len(studio) <= MAX_EXACT_TRACK_SEARCH_QUERIES
    assert len(live) == MAX_EXACT_TRACK_SEARCH_QUERIES
    assert len(studio) == len(set(studio))
    assert all(0 < len(query) <= 300 for query in (*studio, *live))
    assert all('"quoted"' not in query for query in studio)
    assert any(query.endswith("official audio") for query in studio)
    assert any(query.endswith("official video") for query in studio)
    assert any(query.endswith(" live") for query in live)
    assert "Gabry Ponte & KEL quoted Tarantella" in studio
    assert _artist_credit_search_variant("Earth, Wind & Fire") == "Earth, Wind & Fire"


def test_tarantella_cascade_auto_selects_exact_third_party_upload_among_decoys(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(
        session_factory,
        "tarantella-cascade",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    queries = _exact_track_search_queries(intent)
    decoys = [
        _search_metadata(
            "tarantella-live", "Gabry Ponte & KEL - Tarantella (Live)", "Gabry Ponte & KEL"
        ),
        _search_metadata(
            "tarantella-cover", "Gabry Ponte & KEL - Tarantella (Cover)", "Gabry Ponte & KEL"
        ),
        _search_metadata("tarantella-karaoke", "Tarantella Karaoke", "Gabry Ponte & KEL"),
        _search_metadata("tarantella-wrong", "Different Artist - Tarantella", "Different Artist"),
    ]
    exact = _search_metadata("rxw1RCAY3qw", "Gabry Ponte & KEL - Tarantella", "Gabry Ponte & KEL")
    exact["artists"] = ["Gabry Ponte", "KEL"]
    fake = _CascadeYtDlp({queries[0]: [*decoys, decoys[0]], queries[1]: [exact]})
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    assert [call[0] for call in fake.search_calls] == list(queries)
    assert "https://www.youtube.com/watch?v=rxw1RCAY3qw" in fake.probe_calls
    assert 0 < len(fake.probe_calls) <= MAX_SOURCE_DISCOVERY_PROBES
    assert len(fake.probe_calls) == len(set(fake.probe_calls))
    assert all(limit <= MAX_SEARCH_RESULTS_PER_QUERY for _, _, limit in fake.search_calls)
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        selected_row = session.get(SourceCandidate, job.active_source_candidate_id)
        rows = list(
            session.scalars(select(SourceCandidate).where(SourceCandidate.job_id == job_id))
        )
        assert selected_row.source_id == "rxw1RCAY3qw"
        assert selected_row.uploader == "Unrelated Dance Archive"
        assert selected_row.uploader_relationship == "third_party"
        assert len(rows) == len(fake.probe_calls)
        versions = {row.source_id: row.version_signature for row in rows}
        assert versions["rxw1RCAY3qw"] == "studio"
        assert session.scalar(select(ServiceTask.id)) is None
        diagnostics = json.loads(selected_row.sanitized_metadata_json)["discovery_diagnostics"]
        assert diagnostics == {
            "schema_version": 1,
            "query_variant_count": len(queries),
            "query_attempts": [
                {"provider": "youtube", "query": queries[0], "found_count": 5},
                {"provider": "youtube", "query": queries[1], "found_count": 1},
                *[
                    {"provider": "youtube", "query": query, "found_count": 0}
                    for query in queries[2:]
                ],
            ],
            "found_count": 6,
            "probed_count": len(fake.probe_calls),
            "accepted_count": len(rows),
            "rejection_counts": {"duplicate_source_id": 1},
            "stopped_early": False,
        }
        assert "url" not in json.dumps(diagnostics).casefold()
        assert len(diagnostics["query_attempts"]) <= 12
        assert all(
            set(attempt) == {"provider", "query", "found_count"} and len(attempt["query"]) <= 300
            for attempt in diagnostics["query_attempts"]
        )


def test_later_official_query_is_ranked_before_twelve_early_decoys(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, _job_id, lease = _job(
        session_factory,
        "fair-probe-budget",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    queries = _exact_track_search_queries(intent)
    early_decoys = [
        _search_metadata(
            f"early-decoy-{index}",
            f"Different Artist - Tarantella {index}",
            "Different Artist",
        )
        for index in range(MAX_SOURCE_DISCOVERY_PROBES)
    ]
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    fake = _CascadeYtDlp(
        {
            queries[0]: early_decoys[:MAX_SEARCH_RESULTS_PER_QUERY],
            queries[1]: early_decoys[MAX_SEARCH_RESULTS_PER_QUERY:],
            next(query for query in queries if query.endswith("official audio")): [exact],
        }
    )
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    official_query = next(query for query in queries if query.endswith("official audio"))
    official_index = queries.index(official_query)
    assert [call[0] for call in fake.search_calls] == list(queries[: official_index + 1])
    assert "https://www.youtube.com/watch?v=rxw1RCAY3qw" in fake.probe_calls
    assert fake.probe_calls[-1] == "https://www.youtube.com/watch?v=rxw1RCAY3qw"
    assert len(fake.probe_calls) < MAX_SOURCE_DISCOVERY_PROBES


def test_orchestration_evidence_cannot_starve_worker_exact_search(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, _job_id, lease = _job(
        session_factory,
        "evidence-reservation",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    queries = _exact_track_search_queries(intent)
    official_query = next(query for query in queries if query.endswith("official audio"))
    fake = _CascadeYtDlp({official_query: [exact]})
    with session_factory.begin() as session:
        for index in range(MAX_SOURCE_DISCOVERY_PROBES):
            source_id = f"evidence-decoy-{index}"
            session.add(
                EvidenceReference(
                    request_id=request_id,
                    request_track_id=track_id,
                    job_id=lease.job_id,
                    provider="youtube",
                    evidence_kind="provider_search_result",
                    canonical_url=f"https://www.youtube.com/watch?v={source_id}",
                    provider_item_id=source_id,
                    status="available",
                    sanitized_metadata_json="{}",
                )
            )
            fake.full[source_id] = _search_metadata(
                source_id,
                f"Different Artist - Tarantella {index}",
                "Different Artist",
            )
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    assert len([url for url in fake.probe_calls if "evidence-decoy" in url]) == (
        MAX_EVIDENCE_DISCOVERY_PROBES
    )
    assert len(fake.probe_calls) <= MAX_SOURCE_DISCOVERY_PROBES


def test_queue_attached_generic_evidence_is_ranked_instead_of_restored(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, job_id, lease = _job(
        session_factory,
        "generic-active-ranking",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        evidence = EvidenceReference(
            request_id=request_id,
            request_track_id=track_id,
            job_id=job_id,
            provider="youtube",
            evidence_kind="provider_search_result",
            canonical_url="https://www.youtube.com/watch?v=generic-wrong-source",
            provider_item_id="generic-wrong-source",
            status="available",
        )
        session.add(evidence)
        session.flush()
        wrong = _candidate(
            source_id="generic-wrong-source",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=generic-wrong-source",
            request_track_id=track_id,
            evidence_id=evidence.id,
        )
        wrong.job_id = job_id
        wrong.provider_title = "Different Artist - Tarantella"
        wrong.provider_artist = "Different Artist"
        wrong.local_score = 1.0
        session.add(wrong)
        session.flush()
        wrong_id = wrong.id
        # Queue-time attachment is only a convenience, not a durable decision.
        job.active_source_candidate_id = wrong_id
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    fake = _CascadeYtDlp({_exact_track_search_queries(intent)[0]: [exact]})
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        wrong = session.get(SourceCandidate, wrong_id)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "acquisition_source",
                JobDecision.state == "selected",
            )
        )
        assert job is not None and job.active_source_candidate_id == selected.candidate_id
        assert wrong is not None and wrong.local_score < 0.75
        assert decision is not None
        assert "prevalidated_direct_source" not in json.loads(decision.reason_codes_json)


def test_9b8_release_derived_live_snapshot_is_revalidated_and_replaced(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(
        session_factory,
        "legacy-live-source",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    lease.approved_snapshot["version_signature"] = "live"
    lease.approved_snapshot["album"] = "Battiti Live Compilation 2024"
    lease.approved_snapshot["artists"] = []
    lease.approved_snapshot["canonical_identity_verified"] = True
    lease.approved_snapshot["metadata_provenance"] = {
        "automatic_association": True,
        "source": "musicbrainz_search_recordings",
        "request_constraints": {"version_constraint_explicit": False},
    }
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        job.approved_snapshot_json = json.dumps(lease.approved_snapshot)
        track.version_signature = "live"
        legacy = _candidate(
            source_id="legacy-live-upload",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=legacy-live-upload",
            request_track_id=track_id,
        )
        legacy.provider_title = "Gabry Ponte & KEL - Tarantella (Live)"
        legacy.provider_artist = "Gabry Ponte & KEL"
        legacy.duration_seconds = 146.0
        legacy.version_signature = "live"
        legacy.job_id = job_id
        session.add(legacy)
        session.flush()
        job.active_source_candidate_id = legacy.id
        job.source_extractor = legacy.extractor
        job.source_id = legacy.source_id
        record_selected_decision(
            session,
            job,
            category="acquisition_source",
            candidates=[{"source_candidate_id": legacy.id}],
            selected_payload={"source_candidate_id": legacy.id},
            decided_by="deterministic",
            reason_codes=["local_auto_match"],
            prompt_version="source_matcher_v2",
        )
        legacy_id = legacy.id
    intent = SourceIntent(
        artist="Gabry Ponte & KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    fake = _CascadeYtDlp({_exact_track_search_queries(intent)[0]: [exact]})
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    assert lease.approved_snapshot["version_signature"] == "studio"
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        legacy = session.get(SourceCandidate, legacy_id)
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
        assert (
            job is not None
            and json.loads(job.approved_snapshot_json)["version_signature"] == "studio"
        )
        assert track is not None and track.version_signature == "studio"
        assert legacy is not None and legacy.policy_status == "exhausted"
        assert [decision.state for decision in decisions] == ["rejected", "selected"]
        assert decisions[1].prompt_version is None


def test_explicit_live_direct_source_survives_active_candidate_revalidation(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, job_id, lease = _job(
        session_factory,
        "explicit-live-direct",
        artist="Coldplay",
        title="Yellow",
        duration=270.0,
    )
    lease.approved_snapshot.update(
        {
            "requested_version": "live",
            "version_signature": "live",
            "version_constraint_explicit": True,
            "metadata_provenance": {"request_constraints": {"version_constraint_explicit": True}},
        }
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        assert job is not None and track is not None
        job.approved_snapshot_json = json.dumps(lease.approved_snapshot)
        track.version_signature = "live"
        evidence = EvidenceReference(
            request_id=request_id,
            request_track_id=track_id,
            job_id=job_id,
            provider="youtube",
            evidence_kind="direct_user_url",
            canonical_url="https://www.youtube.com/watch?v=yellow-live-direct",
            provider_item_id="yellow-live-direct",
            status="available",
        )
        session.add(evidence)
        session.flush()
        candidate = _candidate(
            source_id="yellow-live-direct",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-live-direct",
            request_track_id=track_id,
            evidence_id=evidence.id,
        )
        candidate.job_id = job_id
        candidate.provider_title = "Coldplay - Yellow (Live)"
        candidate.version_signature = "live"
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        job.active_source_candidate_id = candidate_id

    selected = _resolver(settings, session_factory).resolve(lease, _Monitor(), _Cancellation())

    assert selected.candidate_id == candidate_id
    assert lease.approved_snapshot["version_signature"] == "live"
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        track = session.get(RequestTrack, track_id)
        candidate = session.get(SourceCandidate, candidate_id)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "acquisition_source",
                JobDecision.state == "selected",
            )
        )
        assert job is not None
        assert json.loads(job.approved_snapshot_json)["version_signature"] == "live"
        assert track is not None and track.version_signature == "live"
        assert candidate is not None and candidate.policy_status == "allowed"
        assert decision is not None and decision.decided_by == "deterministic"
        assert json.loads(decision.reason_codes_json) == ["prevalidated_direct_source"]


def test_user_selected_live_source_is_not_retired_by_studio_default(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(
        session_factory,
        "user-selected-live",
        artist="Coldplay",
        title="Yellow",
        duration=270.0,
    )
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        candidate = _candidate(
            source_id="yellow-live-user-choice",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=yellow-live-user-choice",
            request_track_id=track_id,
        )
        candidate.job_id = job_id
        candidate.provider_title = "Coldplay - Yellow (Live)"
        candidate.version_signature = "live"
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        job.active_source_candidate_id = candidate_id
        record_selected_decision(
            session,
            job,
            category="acquisition_source",
            candidates=[{"source_candidate_id": candidate_id}],
            selected_payload={"source_candidate_id": candidate_id},
            decided_by="user",
            reason_codes=["exceptional_review_selection"],
        )

    selected = _resolver(settings, session_factory).resolve(lease, _Monitor(), _Cancellation())

    assert selected.candidate_id == candidate_id
    with session_factory() as session:
        candidate = session.get(SourceCandidate, candidate_id)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.category == "acquisition_source",
                JobDecision.state == "selected",
            )
        )
        assert candidate is not None and candidate.policy_status == "allowed"
        assert candidate.failure_code is None
        assert decision is not None and decision.decided_by == "user"


def test_retry_reusing_strong_candidate_preserves_discovery_diagnostics(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, _job_id, lease = _job(session_factory, "diagnostic-replay")
    previous = {
        "schema_version": 1,
        "query_variant_count": 1,
        "query_attempts": [
            {
                "provider": "youtube",
                "query": "Coldplay Yellow official audio",
                "found_count": 1,
            }
        ],
        "found_count": 1,
        "probed_count": 1,
        "accepted_count": 1,
        "rejection_counts": {},
        "stopped_early": True,
    }
    with session_factory.begin() as session:
        candidate = _candidate(
            source_id="preserved-diagnostic-source",
            provider="youtube",
            extractor="youtube",
            url="https://www.youtube.com/watch?v=preserved-diagnostic-source",
            request_track_id=track_id,
        )
        candidate.sanitized_metadata_json = json.dumps(
            {
                "audio_available": True,
                "audio_quality": 1.0,
                "track": "Yellow",
                "discovery_diagnostics": previous,
            }
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id

    assert (
        _resolver(settings, session_factory)
        .resolve(lease, _Monitor(), _Cancellation())
        .candidate_id
        == candidate_id
    )
    with session_factory() as session:
        candidate = session.get(SourceCandidate, candidate_id)
        assert candidate is not None
        metadata = json.loads(candidate.sanitized_metadata_json)
        assert metadata["discovery_diagnostics"] == previous


def test_failed_evidence_identity_is_not_probed_again_from_search(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, _job_id, lease = _job(
        session_factory,
        "evidence-search-dedup",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    evidence_url = "https://www.youtube.com/watch?v=failed-evidence-source"
    with session_factory.begin() as session:
        session.add(
            EvidenceReference(
                request_id=request_id,
                request_track_id=track_id,
                job_id=lease.job_id,
                provider="youtube",
                evidence_kind="direct_user_url",
                canonical_url=evidence_url,
                provider_item_id="failed-evidence-source",
                status="available",
            )
        )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    duplicate = _search_metadata(
        "failed-evidence-source",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    fake = _FailedEvidenceCascadeYtDlp(
        {query: [duplicate] for query in _exact_track_search_queries(intent)},
        rejected_url=evidence_url,
    )
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    assert fake.probe_calls == [evidence_url]
    with session_factory() as session:
        evidence = session.scalar(
            select(EvidenceReference).where(
                EvidenceReference.provider_item_id == "failed-evidence-source"
            )
        )
        rejected = session.scalar(
            select(SourceCandidate).where(SourceCandidate.source_id == "failed-evidence-source")
        )
        assert evidence is not None and evidence.status == "rejected"
        assert evidence.negative_reason == "probe_validation_rejected"
        assert rejected is not None and rejected.policy_status == "rejected"
        assert rejected.probe_status == "invalid"
        assert rejected.failure_code == "probe_validation_rejected"


def test_nontransient_search_probe_rejection_survives_retry_and_restart(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, _job_id, lease = _job(
        session_factory,
        "search-rejection-replay",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    rejected = _search_metadata(
        "permanently-rejected",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    rejected_url = "https://www.youtube.com/watch?v=permanently-rejected"
    fake = _FailedEvidenceCascadeYtDlp(
        {query: [rejected] for query in _exact_track_search_queries(intent)},
        rejected_url=rejected_url,
    )
    hardened = settings.model_copy(
        update={
            "ai_match_resolution_enabled": False,
            "enabled_media_providers": ["youtube"],
            "media_provider_preference": ["youtube"],
        }
    )

    with pytest.raises(SourceResolutionNeedsReview):
        WorkerSourceResolver(
            hardened,
            session_factory,
            DownloadJobQueue(session_factory),
            fake,  # type: ignore[arg-type]
        ).resolve(lease, _Monitor(), _Cancellation())
    with pytest.raises(SourceResolutionNeedsReview):
        WorkerSourceResolver(
            hardened,
            session_factory,
            DownloadJobQueue(session_factory),
            fake,  # type: ignore[arg-type]
        ).resolve(lease, _Monitor(), _Cancellation())

    assert fake.probe_calls == [rejected_url]
    with session_factory() as session:
        row = session.scalar(
            select(SourceCandidate).where(
                SourceCandidate.request_track_id == track_id,
                SourceCandidate.source_id == "permanently-rejected",
            )
        )
        assert row is not None and row.policy_status == "rejected"
        assert row.probe_status == "invalid"
        assert row.failure_code == "probe_validation_rejected"
        assert "fixture rejected evidence" not in row.sanitized_metadata_json


def test_discovery_probe_budget_and_cross_query_dedup_are_hard_bounds(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(
        session_factory,
        "bounded-cascade",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
    )
    intent = SourceIntent(artist="Gabry Ponte, KEL", title="Tarantella", duration_seconds=146.0)
    queries = _exact_track_search_queries(intent)
    duplicate = _search_metadata("same-result", "Wrong - Tarantella", "Wrong")
    searches: dict[str, list[dict[str, object]]] = {}
    for query_index, query in enumerate(queries):
        searches[query] = [duplicate] + [
            _search_metadata(
                f"wrong-{query_index}-{index}", "Wrong Artist - Tarantella", "Wrong Artist"
            )
            for index in range(MAX_SEARCH_RESULTS_PER_QUERY - 1)
        ]
    fake = _CascadeYtDlp(searches)
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    assert 0 < len(fake.probe_calls) <= MAX_SOURCE_DISCOVERY_PROBES
    assert fake.probe_calls.count("https://www.youtube.com/watch?v=same-result") == 1
    assert len(fake.search_calls) <= MAX_EXACT_TRACK_SEARCH_QUERIES
    assert all(limit <= MAX_SEARCH_RESULTS_PER_QUERY for _, _, limit in fake.search_calls)
    with session_factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(SourceCandidate)
            .where(SourceCandidate.job_id == job_id)
        ) == len(fake.probe_calls)


def test_successful_empty_searches_are_cached_once_per_job_and_diagnostics_are_durable(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(
        session_factory,
        "cached-empty-cascade",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    fake = _CascadeYtDlp({})
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    first_search_count = len(fake.search_calls)
    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    assert first_search_count == len(
        _exact_track_search_queries(
            SourceIntent(
                artist="Gabry Ponte & KEL",
                artists=("Gabry Ponte", "KEL"),
                title="Tarantella",
                duration_seconds=146.0,
            )
        )
    )
    assert len(fake.search_calls) == first_search_count
    assert fake.probe_calls == []
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(EvidenceReference).where(
                    EvidenceReference.job_id == job_id,
                    EvidenceReference.request_track_id == track_id,
                    EvidenceReference.evidence_kind == SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].canonical_url is None and rows[0].provider_item_id is None
        diagnostics = json.loads(rows[0].sanitized_metadata_json)["discovery_diagnostics"]
        assert diagnostics["query_variant_count"] == first_search_count
        assert diagnostics["found_count"] == 0
        assert diagnostics["probed_count"] == 0


def test_transient_probe_retries_use_cached_search_and_cumulative_identity_budget(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, _job_id, lease = _job(
        session_factory,
        "cached-transient-probe",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte & KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    repeated = _search_metadata(
        "temporarily-unavailable",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    searches = {query: [repeated] for query in _exact_track_search_queries(intent)}
    fake = _TransientProbeCascadeYtDlp(searches)
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    first_search_count = len(fake.search_calls)
    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    assert len(fake.search_calls) == first_search_count
    assert len(fake.probe_calls) == 2
    assert len(set(fake.probe_calls)) == 1


def test_retry_probes_untried_cached_candidates_before_transient_retries(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, _job_id, lease = _job(
        session_factory,
        "cached-untried-before-retry",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte & KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    strongest_query = _exact_track_search_queries(intent)[0]
    searches = {
        strongest_query: [
            _search_metadata(
                f"transient-{index}",
                "Gabry Ponte & KEL - Tarantella",
                "Gabry Ponte & KEL",
            )
            for index in range(MAX_SEARCH_RESULTS_PER_QUERY)
        ]
    }
    fake = _TransientProbeCascadeYtDlp(searches)
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    first_search_count = len(fake.search_calls)
    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    assert len(fake.search_calls) == first_search_count
    assert fake.probe_calls == [
        "https://www.youtube.com/watch?v=transient-0",
        "https://www.youtube.com/watch?v=transient-1",
        "https://www.youtube.com/watch?v=transient-2",
        "https://www.youtube.com/watch?v=transient-3",
    ]


def test_probe_budget_is_cumulative_across_resolver_reentry(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, _job_id, lease = _job(
        session_factory,
        "cumulative-probe-budget",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
    )
    intent = SourceIntent(artist="Gabry Ponte & KEL", title="Tarantella", duration_seconds=146.0)
    searches = {
        query: [
            _search_metadata(
                f"wrong-{query_index}-{entry_index}",
                "Different Artist - Tarantella",
                "Different Artist",
            )
            for entry_index in range(MAX_SEARCH_RESULTS_PER_QUERY)
        ]
        for query_index, query in enumerate(_exact_track_search_queries(intent))
    }
    fake = _CascadeYtDlp(searches)
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    assert len(fake.probe_calls) == MAX_SOURCE_DISCOVERY_PROBES
    with pytest.raises(SourceResolutionNeedsReview):
        resolver.resolve(lease, _Monitor(), _Cancellation())
    assert len(fake.probe_calls) == MAX_SOURCE_DISCOVERY_PROBES


def test_provider_fallback_probe_epoch_cannot_consume_the_full_lifetime_cap(
    session_factory: sessionmaker[Session],
) -> None:
    request_id, track_id, _job_id, lease = _job(
        session_factory,
        "fallback-epoch-hard-bound",
    )
    state = SourceDiscoveryState(
        session_factory,
        lease,
        request_id=request_id,
        request_track_id=track_id,
        policy_fingerprint="fallback-epoch-fixture",
    )

    reservations = [
        state.reserve_probe(
            f"youtube:youtube:fallback-{index}",
            maximum_total=MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK,
            epoch="provider_fallback",
            maximum_epoch=MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES,
        )
        for index in range(MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES + 2)
    ]

    assert sum(item.reserved for item in reservations) == MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES
    assert reservations[-1].remaining == 0
    assert reservations[-1].total == MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES
    assert state.probe_total() == MAX_PROVIDER_FALLBACK_DISCOVERY_PROBES


@pytest.mark.parametrize(
    ("monitor", "cancellation", "error"),
    [
        (_Monitor(), _Cancelled(), InterruptedError),
        (_LostMonitor(), _Cancellation(), LeaseLostError),
    ],
)
def test_cascade_checks_cancellation_and_lease_before_provider_calls(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monitor: object,
    cancellation: object,
    error: type[BaseException],
) -> None:
    _request_id, _track_id, _job_id, lease = _job(
        session_factory, "cascade-interrupted", artist="Artist", title="Track", duration=180
    )
    fake = _CascadeYtDlp({})
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )
    with pytest.raises(error):
        resolver.resolve(
            lease,
            monitor,  # type: ignore[arg-type]
            cancellation,  # type: ignore[arg-type]
        )
    assert fake.search_calls == []
    assert fake.probe_calls == []


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
    assert candidate.audio_quality == 0.5


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


def test_provider_fallback_consent_gets_new_bounded_probe_tranche(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(
        session_factory,
        "provider-consent-probe-tranche",
        artist="Gabry Ponte & KEL",
        title="Tarantella",
        duration=146.0,
    )
    constrained_snapshot = {
        **lease.approved_snapshot,
        "requested_provider": "soundcloud",
        "provider_fallback_allowed": False,
    }
    with session_factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None
        job.approved_snapshot_json = json.dumps(constrained_snapshot)
    constrained_lease = JobLease(
        job_id=lease.job_id,
        token=lease.token,
        approved_snapshot=constrained_snapshot,
        retry_count=lease.retry_count,
    )
    fake = _ProviderFallbackTrancheYtDlp()
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["soundcloud", "youtube"],
                "media_provider_preference": ["soundcloud", "youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(SourceResolutionNeedsReview) as raised:
        resolver.resolve(constrained_lease, _Monitor(), _Cancellation())
    assert len(fake.probe_calls) == MAX_SOURCE_DISCOVERY_PROBES
    assert all("soundcloud.com" in url for url in fake.probe_calls)
    assert raised.value.options[0]["fallback_providers"] == ["youtube"]

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
        job.status = "active"
        job.stage = "resolving_source"
        job.lease_token = "fallback-tranche-resume"  # noqa: S105 - inert fixture
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    resumed = JobLease(
        job_id=job_id,
        token="fallback-tranche-resume",  # noqa: S106 - inert fixture
        approved_snapshot=resumed_snapshot,
        retry_count=0,
    )
    selected = resolver.resolve(resumed, _Monitor(), _Cancellation())

    assert selected.provider == "youtube"
    assert selected.source_id == "youtube-fallback-exact"
    assert len(fake.probe_calls) == MAX_SOURCE_DISCOVERY_PROBES + 1
    assert len(fake.probe_calls) <= MAX_SOURCE_DISCOVERY_PROBES_WITH_FALLBACK


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


def test_transient_provider_search_continues_to_safe_provider(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(
        session_factory,
        "transient-provider-fallback",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    queries = _exact_track_search_queries(intent)
    fake = _TransientProviderCascadeYtDlp(
        {queries[0]: [exact]}, transient_provider=ProviderIdentity.SOUNDCLOUD
    )
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["soundcloud", "youtube"],
                "media_provider_preference": ["soundcloud", "youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    assert any(provider is ProviderIdentity.SOUNDCLOUD for _, provider, _ in fake.search_calls)
    assert any(provider is ProviderIdentity.YOUTUBE for _, provider, _ in fake.search_calls)
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        row = session.get(SourceCandidate, selected.candidate_id)
        assert job is not None and row is not None
        warnings = json.loads(job.warnings_json)
        assert [warning["code"] for warning in warnings] == ["source_discovery_transient"]
        diagnostics = json.loads(row.sanitized_metadata_json)["discovery_diagnostics"]
        assert diagnostics["rejection_counts"]["transient_provider_search"] == 1
        assert [call[:2] for call in fake.search_calls] == [
            (queries[0], ProviderIdentity.SOUNDCLOUD),
            (queries[0], ProviderIdentity.YOUTUBE),
        ]


def test_transient_provider_with_only_wrong_alternative_retries_without_review(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, track_id, job_id, lease = _job(
        session_factory,
        "transient-provider-weak-fallback",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    wrong = _search_metadata(
        "wrong-provider-fallback",
        "Different Artist - Tarantella",
        "Different Artist",
    )
    queries = _exact_track_search_queries(intent)
    fake = _TransientProviderCascadeYtDlp(
        {queries[0]: [wrong]}, transient_provider=ProviderIdentity.SOUNDCLOUD
    )
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["soundcloud", "youtube"],
                "media_provider_preference": ["soundcloud", "youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    with pytest.raises(DownloadTimedOut):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        candidate = session.scalar(
            select(SourceCandidate).where(
                SourceCandidate.request_track_id == track_id,
                SourceCandidate.source_id == "wrong-provider-fallback",
            )
        )
        assert job is not None and candidate is not None
        assert job.active_source_candidate_id is None
        assert session.scalar(select(JobDecision.id)) is None
        diagnostics = json.loads(candidate.sanitized_metadata_json)["discovery_diagnostics"]
        assert diagnostics["rejection_counts"]["transient_provider_search"] == len(queries)


def test_transient_evidence_probe_continues_without_negative_cache(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    request_id, track_id, job_id, lease = _job(
        session_factory,
        "transient-evidence-fallback",
        artist="Gabry Ponte, KEL",
        title="Tarantella",
        duration=146.0,
        artists=("Gabry Ponte", "KEL"),
    )
    transient_url = "https://www.youtube.com/watch?v=temporary-evidence"
    with session_factory.begin() as session:
        session.add(
            EvidenceReference(
                request_id=request_id,
                request_track_id=track_id,
                job_id=job_id,
                provider="youtube",
                evidence_kind="provider_search_result",
                canonical_url=transient_url,
                provider_item_id="temporary-evidence",
                status="available",
            )
        )
    intent = SourceIntent(
        artist="Gabry Ponte, KEL",
        artists=("Gabry Ponte", "KEL"),
        title="Tarantella",
        duration_seconds=146.0,
    )
    exact = _search_metadata(
        "rxw1RCAY3qw",
        "Gabry Ponte & KEL - Tarantella",
        "Gabry Ponte & KEL",
    )
    exact["artists"] = ["Gabry Ponte", "KEL"]
    exact["uploader"] = "Gabry Ponte & KEL - Topic"
    queries = _exact_track_search_queries(intent)
    fake = _TransientEvidenceCascadeYtDlp({queries[0]: [exact]}, transient_url=transient_url)
    resolver = WorkerSourceResolver(
        settings.model_copy(
            update={
                "ai_match_resolution_enabled": False,
                "enabled_media_providers": ["youtube"],
                "media_provider_preference": ["youtube"],
            }
        ),
        session_factory,
        DownloadJobQueue(session_factory),
        fake,  # type: ignore[arg-type]
    )

    selected = resolver.resolve(lease, _Monitor(), _Cancellation())

    assert selected.source_id == "rxw1RCAY3qw"
    with session_factory() as session:
        evidence = session.scalar(
            select(EvidenceReference).where(
                EvidenceReference.provider_item_id == "temporary-evidence"
            )
        )
        rejected = session.scalar(
            select(SourceCandidate).where(
                SourceCandidate.request_track_id == track_id,
                SourceCandidate.source_id == "temporary-evidence",
            )
        )
        job = session.get(DownloadJob, job_id)
        row = session.get(SourceCandidate, selected.candidate_id)
        assert evidence is not None and evidence.status == "available"
        assert evidence.negative_reason is None and evidence.negative_until is None
        assert rejected is None
        assert job is not None and row is not None
        assert json.loads(job.warnings_json)[0]["code"] == "source_discovery_transient"
        diagnostics = json.loads(row.sanitized_metadata_json)["discovery_diagnostics"]
        assert diagnostics["rejection_counts"] == {"transient_evidence_probe": 1}


def test_download_cancellation_is_not_recorded_as_transient_provider_failure(
    settings: Settings, session_factory: sessionmaker[Session]
) -> None:
    _request_id, _track_id, job_id, lease = _job(session_factory, "cancelled-provider-search")
    resolver = WorkerSourceResolver(
        settings.model_copy(update={"ai_match_resolution_enabled": False}),
        session_factory,
        DownloadJobQueue(session_factory),
        _CancelledYtDlp(),  # type: ignore[arg-type]
    )

    with pytest.raises(DownloadCancelled):
        resolver.resolve(lease, _Monitor(), _Cancellation())

    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job is not None and json.loads(job.warnings_json) == []


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
        assert json.loads(job.warnings_json) == [
            {
                "code": "source_discovery_transient",
                "message": "12 temporary media source check(s) failed; other safe alternatives "
                "were attempted.",
            }
        ]
