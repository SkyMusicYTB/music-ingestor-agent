"""Offline full-job regressions: fake providers, real queue and atomic publication."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest
from PIL import Image
from sqlalchemy import select

from app.clients.ytdlp import DownloadResult
from app.db.models import (
    Conversation,
    DownloadJob,
    EvidenceReference,
    JobDecision,
    JobReviewOption,
    Request,
    RequestTrack,
    SourceCandidate,
    Track,
    User,
)
from app.repositories.decisions import (
    DecisionSelection,
    apply_review_bundle,
    review_bundle_fingerprint,
)
from app.repositories.jobs import JobRepository
from app.services.artwork import normalize_artwork
from app.services.duplicates import normalize_text
from app.workers.media import MediaProbe
from app.workers.metadata import CanonicalMetadataResolution, WorkerMetadataError
from app.workers.processor import DownloadJobProcessor
from app.workers.provider_fallback import FALLBACK_WARNING
from app.workers.queue import DownloadJobQueue

URL = "https://www.youtube.com/watch?v=rxw1RCAY3qw"
METADATA = {
    "id": "rxw1RCAY3qw",
    "extractor": "youtube",
    "artist": "Gabry Ponte, KEL",
    "artists": ["Gabry Ponte", "KEL"],
    "track": "Tarantella",
    "title": "Gabry Ponte, KEL - Tarantella (Official Audio)",
    "uploader": "Gabry Ponte",
    "duration": 146.0,
    "release_year": 2024,
    "album": "Tarantella",
    "thumbnail": "https://i.ytimg.com/vi/rxw1RCAY3qw/hqdefault.jpg",
}


class Provider:
    def __init__(self, metadata=None):
        self.metadata = dict(METADATA if metadata is None else metadata)
        self.downloads = 0

    def probe(self, url, **kwargs):
        assert url == URL
        return dict(self.metadata)

    def download_audio(self, url, staging, **kwargs):
        assert url == URL
        self.downloads += 1
        path = staging / "youtube-rxw1RCAY3qw.opus"
        path.write_bytes(b"offline synthetic audio placeholder")
        return DownloadResult(path, "youtube", "rxw1RCAY3qw", self.metadata)


class Media:
    def normalize_and_verify(self, path, **kwargs):
        return MediaProbe(path, "opus", ("ogg",), 146.0, 128000)


class MusicBrainz:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def resolve(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return CanonicalMetadataResolution(
            "reject", None, (), "MusicBrainz returned no recording candidates", "no_candidates"
        )


def queued_job(factory, *, direct=True, score=1.0):
    with factory.begin() as session:
        user = User(username="fixture", username_normalized="fixture", password_hash="fixture")  # noqa: S106
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="fixture")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text=URL,
            action="add",
            idempotency_key="metadata-pipeline",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(
            request_id=request.id,
            ordinal=1,
            artist="Gabry Ponte, KEL",
            title="Tarantella",
            duration_seconds=146.0,
            selected=True,
            source_extractor="youtube",
            source_id="rxw1RCAY3qw",
            canonical_identity_verified=False,
            recording_mbid="11111111-1111-1111-1111-111111111111",
            release_mbid="22222222-2222-2222-2222-222222222222",
            release_group_mbid="33333333-3333-3333-3333-333333333333",
        )
        session.add(track)
        session.flush()
        evidence = EvidenceReference(
            request_id=request.id,
            request_track_id=track.id,
            provider="youtube",
            evidence_kind="direct_user_url" if direct else "provider_search_result",
            canonical_url=URL,
            status="available",
        )
        session.add(evidence)
        session.flush()
        source = SourceCandidate(
            evidence_id=evidence.id,
            request_track_id=track.id,
            provider="youtube",
            extractor="youtube",
            source_id="rxw1RCAY3qw",
            acquisition_url=URL,
            provider_title=METADATA["title"],
            provider_artist=METADATA["artist"],
            uploader="Gabry Ponte",
            duration_seconds=146.0,
            group_key="fixture",
            local_score=score,
            policy_status="allowed",
            probe_status="valid",
        )
        session.add(source)
        session.flush()
        user_id, request_id, track_id, source_id = user.id, request.id, track.id, source.id
    jobs = JobRepository(factory).queue_approved(request_id, user_id, [track_id])
    return jobs[0], source_id, user_id


def processor(settings, factory, monkeypatch, *, provider=None, resolver=None):
    settings.min_free_bytes = 0
    settings.max_media_bytes = 1024 * 1024
    settings.ai_match_resolution_enabled = False
    captured = {}

    def tag(path, values, artwork, **kwargs):
        captured.update(values)

    monkeypatch.setattr("app.workers.processor.write_tags", tag)

    class Scanner:
        def index_one(self, path):
            with factory.begin() as session:
                row = Track(
                    filepath=path.relative_to(settings.music_path).as_posix(),
                    artist=captured["artist"],
                    title=captured["title"],
                    artist_normalized=normalize_text(captured["artist"]),
                    title_normalized=normalize_text(captured["title"]),
                    duration_seconds=146.0,
                    file_mtime_ns=path.stat().st_mtime_ns,
                    file_size=path.stat().st_size,
                    source_extractor="youtube",
                    source_id="rxw1RCAY3qw",
                    recording_mbid=captured.get("recording_mbid"),
                    release_mbid=captured.get("release_mbid"),
                    release_group_mbid=captured.get("release_group_mbid"),
                    provenance_json=json.dumps(captured["metadata_provenance"]),
                )
                session.add(row)
                session.flush()
                return row

    provider = provider or Provider()
    resolver = resolver or MusicBrainz()
    queue = DownloadJobQueue(factory)
    worker = DownloadJobProcessor(
        settings=settings,
        queue=queue,
        ytdlp=provider,
        media=Media(),
        session_factory=factory,
        library_scanner=Scanner(),
        metadata_resolver=resolver,
    )
    return worker, provider, resolver, captured


def decide_provider(factory, job_id):
    with factory.begin() as session:
        job = session.get(DownloadJob, job_id)
        decision = session.scalar(
            select(JobDecision).where(
                JobDecision.job_id == job_id,
                JobDecision.state == "pending",
                JobDecision.category == "canonical_metadata",
            )
        )
        assert decision is not None
        option = session.scalar(
            select(JobReviewOption).where(JobReviewOption.decision_id == decision.id)
        )
        assert option is not None
        bundle = dict(
            bundle_fingerprint=review_bundle_fingerprint([decision]),
            revision=job.decision_revision,
            selections=[DecisionSelection(decision.id, option.id)],
        )
        apply_review_bundle(session, job, **bundle)
        assert apply_review_bundle(session, job, **bundle).replayed


def test_direct_prefer_publishes_without_mbid_and_keeps_source(
    settings, session_factory, monkeypatch
):
    job_id, source_id, _ = queued_job(session_factory)
    worker, provider, resolver, tags = processor(settings, session_factory, monkeypatch)
    outcome = worker.process(worker.queue.claim_next())
    assert outcome.status == "completed"
    assert provider.downloads == resolver.calls == 1
    assert tags["canonical_identity_verified"] is False
    assert tags["metadata_authority"] == "direct_user_source"
    assert tags["artists"] == ["Gabry Ponte", "KEL"]
    assert all(
        tags[key] is None for key in ("recording_mbid", "release_mbid", "release_group_mbid")
    )
    assert (settings.music_path / outcome.relative_path).is_file()
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job.active_source_candidate_id == source_id
        assert json.loads(job.warnings_json) == [
            {"code": "provider_metadata_fallback", "message": FALLBACK_WARNING}
        ]
        assert job.error_message is None
        row = session.get(Track, job.final_track_id)
        assert row.source_id == "rxw1RCAY3qw" and row.recording_mbid is None
        assert session.scalar(select(JobDecision.id).where(JobDecision.state == "pending")) is None


def test_require_review_before_download_and_durable_acceptance(
    settings, session_factory, monkeypatch
):
    settings.canonical_metadata_policy = "require"
    job_id, source_id, _ = queued_job(session_factory)
    worker, provider, resolver, tags = processor(settings, session_factory, monkeypatch)
    assert worker.process(worker.queue.claim_next()).status == "needs_review"
    assert provider.downloads == 0
    with session_factory() as session:
        job = session.get(DownloadJob, job_id)
        assert job.stage == "resolving_metadata" and job.active_source_candidate_id == source_id
        assert "source" not in (job.error_code or "")
    decide_provider(session_factory, job_id)
    assert worker.process(worker.queue.claim_next()).status == "completed"
    assert provider.downloads == 1 and resolver.calls == 1
    assert tags["metadata_authority"] == "user_confirmed_provider_metadata"
    assert tags["recording_mbid"] is None


@pytest.mark.parametrize("score,expected", [(0.91, "completed"), (0.80, "needs_review")])
def test_automatic_fallback_keeps_conservative_threshold(
    settings, session_factory, monkeypatch, score, expected
):
    queued_job(session_factory, direct=False, score=score)
    worker, provider, _, tags = processor(settings, session_factory, monkeypatch)
    assert worker.process(worker.queue.claim_next()).status == expected
    assert provider.downloads == (1 if expected == "completed" else 0)
    if tags:
        assert tags["metadata_authority"] == "validated_provider"


@pytest.mark.parametrize(
    "policy,status,downloads", [("prefer", "completed", 1), ("require", "retry_wait", 0)]
)
def test_provider_outage_is_not_source_mismatch(
    settings, session_factory, monkeypatch, policy, status, downloads
):
    settings.canonical_metadata_policy = policy
    job_id, source_id, _ = queued_job(session_factory)
    resolver = MusicBrainz(WorkerMetadataError("temporary MusicBrainz outage"))
    worker, provider, _, _ = processor(settings, session_factory, monkeypatch, resolver=resolver)
    assert worker.process(worker.queue.claim_next()).status == status
    assert provider.downloads == downloads
    with session_factory() as session:
        assert session.get(DownloadJob, job_id).active_source_candidate_id == source_id
        assert session.scalar(select(JobDecision.id).where(JobDecision.state == "pending")) is None


def test_unknown_probe_duration_reuses_audio_after_metadata_review(
    settings, session_factory, monkeypatch
):
    settings.canonical_metadata_policy = "require"
    job_id, _, _ = queued_job(session_factory)
    provider = Provider({**METADATA, "duration": None})
    worker, _, resolver, _tags = processor(
        settings, session_factory, monkeypatch, provider=provider
    )
    assert worker.process(worker.queue.claim_next()).status == "needs_review"
    assert provider.downloads == 1
    assert job_id in worker.queue.staging_job_ids_to_preserve()
    decide_provider(session_factory, job_id)
    # A new processor represents service restart; audio and decision survive both.
    restarted, _, _, _ = processor(
        settings, session_factory, monkeypatch, provider=provider, resolver=resolver
    )
    assert restarted.process(restarted.queue.claim_next()).status == "completed"
    assert provider.downloads == 1 and resolver.calls == 1


def test_post_download_retry_reuses_valid_audio(settings, session_factory, monkeypatch):
    job_id, _, _ = queued_job(session_factory)
    worker, provider, resolver, _ = processor(settings, session_factory, monkeypatch)
    original = worker._fetch_artwork
    monkeypatch.setattr(
        worker,
        "_fetch_artwork",
        lambda *args: (_ for _ in ()).throw(OSError("temporary disk issue")),
    )
    assert worker.process(worker.queue.claim_next()).status == "retry_wait"
    with session_factory.begin() as session:
        session.get(DownloadJob, job_id).available_at = datetime.now(UTC)
    monkeypatch.setattr(worker, "_fetch_artwork", original)
    assert worker.process(worker.queue.claim_next()).status == "completed"
    assert provider.downloads == resolver.calls == 1


@pytest.mark.parametrize("version", ["Cover", "Karaoke", "Remix", "Live"])
def test_wrong_versions_never_reach_fallback_or_download(
    settings, session_factory, monkeypatch, version
):
    queued_job(session_factory)
    provider = Provider({**METADATA, "title": f"Gabry Ponte, KEL - Tarantella ({version})"})
    worker, _, resolver, tags = processor(settings, session_factory, monkeypatch, provider=provider)
    # Exhaustion is a clear failure, not an empty pending source review.
    worker.settings.max_automatic_source_attempts = 1
    assert worker.process(worker.queue.claim_next()).status == "failed"
    assert provider.downloads == resolver.calls == 0
    assert tags == {}


def test_fallback_uses_safe_source_thumbnail(settings, session_factory, monkeypatch):
    job_id, _, _ = queued_job(session_factory)
    worker, _, _, _ = processor(settings, session_factory, monkeypatch)
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(image, format="PNG")
    artwork = normalize_artwork(image.getvalue())
    fetched = []

    class ArtworkProvider:
        def fetch(self, url):
            fetched.append(url)
            return artwork

    worker.artwork_fetcher = ArtworkProvider()
    assert worker.process(worker.queue.claim_next()).status == "completed"
    assert fetched == [METADATA["thumbnail"]]
    with session_factory() as session:
        messages = json.loads(session.get(DownloadJob, job_id).warnings_json)
        assert sum(item["code"] == "provider_metadata_fallback" for item in messages) == 1
        assert sum(item["code"] == "youtube_thumbnail_artwork" for item in messages) == 1
    assert len(list(settings.music_path.rglob("cover.jpg"))) == 1


def test_duplicate_checks_without_mbid_prevent_another_download(
    settings, session_factory, monkeypatch
):
    queued_job(session_factory)
    existing_path = settings.music_path / "existing.opus"
    existing_path.write_bytes(b"already owned offline fixture")
    with session_factory.begin() as session:
        session.add(
            Track(
                artist=METADATA["artist"],
                title="Tarantella",
                artist_normalized=normalize_text(METADATA["artist"]),
                title_normalized="tarantella",
                filepath="existing.opus",
                duration_seconds=146.0,
                file_mtime_ns=existing_path.stat().st_mtime_ns,
                file_size=existing_path.stat().st_size,
                source_id="rxw1RCAY3qw",
                source_extractor="youtube",
                recording_mbid=None,
            )
        )
    worker, provider, _, tags = processor(settings, session_factory, monkeypatch)
    outcome = worker.process(worker.queue.claim_next())
    assert outcome.status == "completed" and outcome.relative_path == "existing.opus"
    assert provider.downloads == 0 and tags == {}
    assert existing_path.read_bytes() == b"already owned offline fixture"


def test_failed_job_retry_reuses_completed_audio(settings, session_factory, monkeypatch):
    job_id, _, user_id = queued_job(session_factory)
    worker, provider, resolver, _ = processor(settings, session_factory, monkeypatch)
    original = worker._fetch_artwork
    monkeypatch.setattr(
        worker,
        "_fetch_artwork",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bounded failure")),
    )
    assert worker.process(worker.queue.claim_next()).status == "failed"
    assert job_id in worker.queue.staging_job_ids_to_preserve()
    JobRepository(session_factory).mutate_for_user(job_id, user_id, "retry")
    monkeypatch.setattr(worker, "_fetch_artwork", original)
    assert worker.process(worker.queue.claim_next()).status == "completed"
    assert provider.downloads == resolver.calls == 1
