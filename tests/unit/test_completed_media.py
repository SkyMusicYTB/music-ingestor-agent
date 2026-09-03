from __future__ import annotations

import json
import os
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.db.models import Conversation, DownloadJob, JobArtifact, Request, RequestTrack, User
from app.services.filesystem import create_staging_directory
from app.workers.completed_media import CompletedMediaStore, expire_review_media
from app.workers.queue import DownloadJobQueue, JobLease, LeaseLostError


@pytest.fixture
def completed_media(session_factory, settings):
    token = secrets.token_hex(16)
    with session_factory.begin() as session:
        user = User(
            username="media-owner",
            username_normalized="media-owner",
            password_hash="offline-fixture",  # noqa: S106 - inert fixture
        )
        session.add(user)
        session.flush()
        conversation = Conversation(user_id=user.id, title="Retained offline fixture")
        session.add(conversation)
        session.flush()
        request = Request(
            user_id=user.id,
            conversation_id=conversation.id,
            raw_text="add offline test",
            action="add",
            idempotency_key="completed-media-fixture",
        )
        session.add(request)
        session.flush()
        track = RequestTrack(request_id=request.id, ordinal=1, artist="Fixture", title="Audio")
        session.add(track)
        session.flush()
        job = DownloadJob(
            request_track_id=track.id,
            dedup_key="completed-media-fixture",
            status="active",
            stage="verifying",
            approved_snapshot_json="{}",
            lease_token=token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    staging = create_staging_directory(settings.downloads_path, job_id)
    path = staging / "fixture.opus"
    path.write_bytes(b"offline-completed-media")
    store = CompletedMediaStore(session_factory, max_bytes=1024)
    lease = JobLease(job_id, token, {}, 0)
    source = {"extractor": "youtube", "source_id": "rxw1RCAY3qw"}
    store.save(lease, path, staging, **source)
    with session_factory() as session:
        artifact_id = session.scalar(select(JobArtifact.id).where(JobArtifact.job_id == job_id))
    return SimpleNamespace(
        factory=session_factory,
        root=settings.downloads_path,
        staging=staging,
        path=path,
        store=store,
        lease=lease,
        source=source,
        artifact_id=artifact_id,
    )


def test_completed_file_is_reused_under_renewed_lease_without_duplicate_artifact(completed_media):
    item = completed_media
    assert item.store.find(item.lease, item.staging, **item.source) == item.path
    newer = replace(item.lease, token=secrets.token_hex(16))
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).lease_token = newer.token
    assert item.store.find(newer, item.staging, **item.source) == item.path
    item.store.save(newer, item.path, item.staging, **item.source)
    with item.factory() as session:
        assert session.scalar(select(func.count()).select_from(JobArtifact)) == 1
        artifact = session.get(JobArtifact, item.artifact_id)
        assert artifact.generation_token == newer.token
        assert artifact.size_bytes == item.path.stat().st_size
        assert json.loads(artifact.metadata_json)["source_id"] == "rxw1RCAY3qw"


@pytest.mark.parametrize("mutation", ["same_size_hash_change", "size_change", "missing"])
def test_completed_file_changed_or_missing_is_never_reused(completed_media, mutation):
    item = completed_media
    if mutation == "same_size_hash_change":
        item.path.write_bytes(b"X" * item.path.stat().st_size)
    elif mutation == "size_change":
        item.path.write_bytes(b"longer" * 20)
    else:
        item.path.unlink()
    assert item.store.find(item.lease, item.staging, **item.source) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_sha256", None),
        ("content_sha256", "bad-digest"),
        ("size_bytes", None),
        ("size_bytes", 0),
        ("size_bytes", 1025),
        ("metadata_json", "not-json"),
        ("metadata_json", "null"),
    ],
)
def test_incomplete_or_malformed_artifact_is_not_reused(completed_media, field, value):
    item = completed_media
    with item.factory.begin() as session:
        setattr(session.get(JobArtifact, item.artifact_id), field, value)
    assert item.store.find(item.lease, item.staging, **item.source) is None


@pytest.mark.parametrize("relative_path", ["../outside.opus", "/outside.opus", "nested/file.opus"])
def test_retained_artifact_cannot_escape_flat_job_directory(completed_media, relative_path):
    item = completed_media
    outside = item.root / "outside.opus"
    outside.write_bytes(item.path.read_bytes())
    with item.factory.begin() as session:
        session.get(JobArtifact, item.artifact_id).relative_path = relative_path
    assert item.store.find(item.lease, item.staging, **item.source) is None
    assert outside.read_bytes() == b"offline-completed-media"
    with pytest.raises(ValueError, match="job-local regular file"):
        item.store.save(item.lease, outside, item.staging, **item.source)


@pytest.mark.parametrize("link_kind", ["file_symlink", "directory_symlink", "hard_link"])
def test_completed_media_rejects_links_without_touching_target(completed_media, link_kind):
    item = completed_media
    original = item.path.read_bytes()
    outside = item.root / "outside.opus"
    if link_kind == "directory_symlink":
        physical = item.staging.with_name("physical-fixture")
        item.staging.rename(physical)
        item.staging.symlink_to(physical, target_is_directory=True)
        target = physical / item.path.name
    else:
        outside.write_bytes(original)
        item.path.unlink()
        if link_kind == "file_symlink":
            item.path.symlink_to(outside)
        else:
            os.link(outside, item.path)
        target = outside
    assert item.store.find(item.lease, item.staging, **item.source) is None
    with pytest.raises(ValueError, match="job-local regular file"):
        item.store.save(item.lease, item.path, item.staging, **item.source)
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "source",
    [
        {"extractor": "youtube", "source_id": "different-upload"},
        {"extractor": "soundcloud", "source_id": "rxw1RCAY3qw"},
    ],
)
def test_completed_media_is_bound_to_exact_provider_source(completed_media, source):
    item = completed_media
    assert item.store.find(item.lease, item.staging, **source) is None


def test_expired_artifact_is_not_reused_or_advertised_ready(completed_media):
    item = completed_media
    with item.factory.begin() as session:
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert not item.store.has_ready(item.lease.job_id)
    assert item.store.find(item.lease, item.staging, **item.source) is None
    assert item.path.exists()


@pytest.mark.parametrize("lease_change", ["wrong_token", "expired", "cancel_requested"])
def test_completed_media_find_and_save_are_lease_fenced(completed_media, lease_change):
    item = completed_media
    with item.factory.begin() as session:
        job = session.get(DownloadJob, item.lease.job_id)
        if lease_change == "wrong_token":
            job.lease_token = secrets.token_hex(16)
        elif lease_change == "expired":
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        else:
            job.status = "cancel_requested"
    with pytest.raises(LeaseLostError):
        item.store.find(item.lease, item.staging, **item.source)
    with pytest.raises(LeaseLostError):
        item.store.save(item.lease, item.path, item.staging, **item.source)
    with item.factory() as session:
        assert session.get(JobArtifact, item.artifact_id).generation_token == item.lease.token


def test_find_rechecks_lease_after_hashing(completed_media, monkeypatch):
    import app.workers.completed_media as module

    item = completed_media
    original_hash = module.sha256_file

    def changed_lease_after_hash(path):
        digest = original_hash(path)
        with item.factory.begin() as session:
            session.get(DownloadJob, item.lease.job_id).lease_token = secrets.token_hex(16)
        return digest

    monkeypatch.setattr(module, "sha256_file", changed_lease_after_hash)
    with pytest.raises(LeaseLostError):
        item.store.find(item.lease, item.staging, **item.source)


@pytest.mark.parametrize("state", ["needs_review", "failed", "cancelled", "completed"])
def test_expired_terminal_or_review_staging_is_removed_once(completed_media, state):
    item = completed_media
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = state
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert expire_review_media(item.factory, item.root) == 1
    assert not item.staging.exists()
    with item.factory() as session:
        assert session.get(JobArtifact, item.artifact_id).status == "removed"
        assert session.get(DownloadJob, item.lease.job_id).status == state
    assert expire_review_media(item.factory, item.root) == 0


@pytest.mark.parametrize(
    "state", ["active", "queued", "retry_wait", "waiting_for_space", "cancel_requested"]
)
def test_expired_staging_is_retained_while_job_can_resume(completed_media, state):
    item = completed_media
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = state
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert expire_review_media(item.factory, item.root) == 0
    assert item.path.read_bytes() == b"offline-completed-media"
    with item.factory() as session:
        assert session.get(JobArtifact, item.artifact_id).status == "ready"


def test_fresh_review_retention_does_not_delete_source_or_library(completed_media, settings):
    item = completed_media
    published = settings.music_path / "published.opus"
    published.write_bytes(b"published audio is independent")
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = "needs_review"
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=6
        )
    assert expire_review_media(item.factory, item.root) == 0
    assert item.path.read_bytes() == b"offline-completed-media"
    assert published.read_bytes() == b"published audio is independent"


def test_cleanup_never_follows_replaced_staging_directory(completed_media):
    item = completed_media
    target = item.root / "outside-library"
    item.staging.rename(target)
    item.staging.symlink_to(target, target_is_directory=True)
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = "needs_review"
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert expire_review_media(item.factory, item.root) == 0
    assert (target / item.path.name).read_bytes() == b"offline-completed-media"
    with item.factory() as session:
        assert session.get(JobArtifact, item.artifact_id).status == "ready"


def test_expiry_of_one_artifact_does_not_delete_fresh_media_for_same_job(completed_media):
    item = completed_media
    fresh = item.staging / "newer-source.opus"
    fresh.write_bytes(b"newer completed source")
    item.store.save(item.lease, fresh, item.staging, extractor="youtube", source_id="newer")
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = "needs_review"
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert expire_review_media(item.factory, item.root) == 0
    assert fresh.read_bytes() == b"newer completed source"
    assert item.path.read_bytes() == b"offline-completed-media"
    with item.factory() as session:
        assert set(session.scalars(select(JobArtifact.status))) == {"ready"}


def test_failed_job_retains_completed_audio_for_retry_only_within_retention(completed_media):
    item = completed_media
    with item.factory.begin() as session:
        session.get(DownloadJob, item.lease.job_id).status = "failed"
    queue = DownloadJobQueue(item.factory)
    assert item.lease.job_id in queue.staging_job_ids_to_preserve()
    assert expire_review_media(item.factory, item.root) == 0
    with item.factory.begin() as session:
        session.get(JobArtifact, item.artifact_id).updated_at = datetime.now(UTC) - timedelta(
            days=8
        )
    assert item.lease.job_id not in queue.staging_job_ids_to_preserve()
    assert expire_review_media(item.factory, item.root) == 1
    assert not item.path.exists()
