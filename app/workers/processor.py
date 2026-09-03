from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
    DownloadProgress,
    DownloadResult,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
    validate_public_media_metadata,
)
from app.config import Settings
from app.db.enums import JobStage
from app.db.models import DownloadJob, EvidenceReference, RequestTrack, ServiceTask, Track
from app.db.models import SourceCandidate as DbSourceCandidate
from app.logging import redact
from app.repositories.decisions import (
    candidate_set_fingerprint,
    latest_canonical_selection,
    record_selected_decision,
    selected_decision,
    selected_payload,
)
from app.repositories.library import LibraryRepository
from app.services.artist_credits import structured_artists
from app.services.artwork import (
    Artwork,
    ArtworkCacheService,
    ArtworkError,
    ArtworkFetcher,
    artwork_as_jpeg,
    cover_art_archive_urls,
    youtube_thumbnail_url,
)
from app.services.duplicates import (
    DuplicateCandidate,
    DuplicateDetector,
    strip_provider_suffixes,
)
from app.services.filesystem import (
    DestinationExistsError,
    InsufficientSpaceError,
    PublicationResult,
    add_source_collision_suffix,
    build_track_relative_path,
    create_staging_directory,
    ensure_free_space,
    publish_album_cover_no_clobber,
    publish_no_clobber,
    sha256_file,
)
from app.services.library_scan import LibraryScanner
from app.services.metadata_matching import (
    MetadataCandidate,
    MetadataMatcher,
)
from app.services.metadata_matching import (
    normalize_text as normalize_metadata_text,
)
from app.services.source_selection import SelectionDecision, TrackIntent
from app.sources import (
    DEFAULT_VERSION_CLASSIFIER,
    CanonicalMatchDecision,
    MatchDecision,
    provider_for_extractor,
    provider_for_url,
    resolve_provider_recording_metadata,
    validate_canonical_match_decision,
)
from app.tags import TaggingError, UnsupportedMediaFormat, write_tags
from app.tools.youtube import YouTubeTool
from app.workers.ai_task_reuse import reuse_or_create_decision_task
from app.workers.cleanup import cleanup_staging_directory
from app.workers.completed_media import CompletedMediaStore
from app.workers.lease import LeaseMonitor
from app.workers.media import MediaProbe, MediaProcessor, MediaValidationError
from app.workers.metadata import MusicBrainzWorkerResolver, WorkerMetadataError
from app.workers.provider_fallback import (
    FALLBACK_AUTHORITIES,
    FALLBACK_WARNING,
    SourceAuthority,
    provider_fallback,
)
from app.workers.queue import (
    DownloadJobQueue,
    JobCancellationRequested,
    JobLease,
    LeaseLostError,
)
from app.workers.reservations import (
    BudgetGuardSignal,
    MediaBudgetExceeded,
    MediaReservationManager,
    Reservation,
)
from app.workers.source_failures import (
    is_transient_source_error as _is_transient_source_error,
)
from app.workers.source_resolution import (
    SourceResolutionNeedsReview,
    WorkerSourceResolver,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    job_id: str
    status: str
    relative_path: str | None = None


class SourceNeedsReview(RuntimeError):
    def __init__(self, decision: SelectionDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class JobNeedsReview(RuntimeError):
    def __init__(
        self,
        reason: str,
        options: list[dict[str, Any]] | None = None,
        *,
        category: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.options = options or []
        self.category = category


class SourceCandidateRejected(JobNeedsReview):
    """A source-specific identity failure that permits bounded automatic fallback."""


class DuplicateOwned(RuntimeError):
    def __init__(self, track: Track, sha256: str) -> None:
        super().__init__("the requested track is already present")
        self.track = track
        self.sha256 = sha256


class PublicationConflict(RuntimeError):
    pass


class InitialLibraryScanPending(RuntimeError):
    """Acquisition must wait for a covered baseline without spending retries."""


class _FileContentCollision(RuntimeError):
    pass


class DownloadJobProcessor:
    def __init__(
        self,
        *,
        settings: Settings,
        queue: DownloadJobQueue,
        ytdlp: YtDlpClient,
        youtube: YouTubeTool | None = None,
        artwork_fetcher: ArtworkCacheService | ArtworkFetcher | None = None,
        media: MediaProcessor | None = None,
        session_factory: sessionmaker[Session] | None = None,
        library_scanner: LibraryScanner | None = None,
        shutdown_signal: CancellationSignal | None = None,
        metadata_resolver: MusicBrainzWorkerResolver | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        self.ytdlp = ytdlp
        self.youtube = youtube or YouTubeTool(
            ytdlp, max_duration_seconds=settings.max_direct_media_seconds
        )
        self.artwork_fetcher = artwork_fetcher
        self.media = media
        self.session_factory = session_factory
        self.library_scanner = library_scanner
        self.shutdown_signal = shutdown_signal
        self.metadata_resolver = metadata_resolver
        self.metadata_matcher = MetadataMatcher()
        self.duplicate_detector = DuplicateDetector(settings.music_path)
        self.reservations = (
            MediaReservationManager(
                session_factory,
                min_free_bytes=settings.min_free_bytes,
                max_media_bytes=settings.max_media_bytes,
            )
            if session_factory is not None
            else None
        )
        self.source_resolver = (
            WorkerSourceResolver(settings, session_factory, queue, ytdlp)
            if session_factory is not None
            else None
        )
        self.completed_media = (
            CompletedMediaStore(session_factory, settings.max_media_bytes)
            if session_factory is not None
            else None
        )

    def process(self, lease: JobLease) -> ProcessOutcome:
        staging: Path | None = None
        publication: PublicationResult | None = None
        download_reservation: Reservation | None = None
        publication_reservation: Reservation | None = None
        monitor = LeaseMonitor(self.queue, lease)
        try:
            with monitor:
                self._ensure_library_ready()
                self.queue.clear_library_wait(lease)
                self.settings.downloads_path.mkdir(parents=True, exist_ok=True)
                ensure_free_space(
                    self.settings.downloads_path,
                    required_bytes=0,
                    reserve_bytes=self.settings.min_free_bytes,
                )
                staging = create_staging_directory(self.settings.downloads_path, lease.job_id)
                base_cancellation = _CombinedCancellation(
                    monitor.cancel_event, self.shutdown_signal
                )
                if self.reservations is not None:
                    download_reservation = self.reservations.reserve_download(
                        lease.job_id,
                        lease.token,
                        staging,
                    )
                    cancellation: CancellationSignal = BudgetGuardSignal(
                        base_cancellation,
                        self.reservations,
                        download_reservation,
                        staging,
                    )
                else:
                    cancellation = base_cancellation
                result, _source_url, media_probe, tag_values = self._acquire_valid_source(
                    lease,
                    monitor,
                    cancellation,
                    staging,
                    download_reservation,
                )
                self._check_duplicate(lease.job_id, tag_values, media_probe)
                artwork = self._fetch_artwork(lease, monitor, result.metadata, tag_values)
                self.queue.set_progress(lease, stage=JobStage.TAGGING, progress=0.76)
                write_tags(media_probe.path, tag_values, artwork, verify=True)
                self._save_completed_media(lease, media_probe.path, staging, tag_values)
                monitor.raise_if_unusable()
                self.queue.set_progress(lease, stage=JobStage.VERIFYING, progress=0.86)
                relative_path = build_track_relative_path(
                    artist=str(tag_values["artist"]),
                    title=str(tag_values["title"]),
                    album=_string_or_none(tag_values.get("album")),
                    track_number=_int_or_none(tag_values.get("track_number")),
                    extension=media_probe.path.suffix,
                    year=_int_or_none(tag_values.get("year")),
                    disc_number=_int_or_none(tag_values.get("disc_number")),
                    disc_total=_int_or_none(tag_values.get("disc_total")),
                )
                ensure_free_space(
                    self.settings.music_path,
                    required_bytes=media_probe.path.stat().st_size,
                    reserve_bytes=self.settings.min_free_bytes,
                )
                monitor.raise_if_unusable()
                self.queue.heartbeat(lease)
                self.queue.set_progress(lease, stage=JobStage.PUBLISHING, progress=0.94)
                if self.reservations is not None:
                    publication_reservation = self.reservations.reserve_publication(
                        lease.job_id,
                        lease.token,
                        self.settings.music_path,
                        media_probe.path.stat().st_size,
                    )
                publication = self._publish_or_adopt(
                    media_probe.path,
                    relative_path,
                    source_id=_string_or_none(tag_values.get("source_id")),
                )
                self._publish_cover_sidecar(artwork, publication)
                try:
                    final_track_id = self._index_publication(publication)
                except Exception as exc:
                    # The tagged file is already atomically visible. Leave it in
                    # place and let the periodic/startup scanner reconcile it.
                    final_track_id = None
                    logger.exception(
                        "published file could not be indexed immediately",
                        extra={"job_id": lease.job_id},
                    )
                    try:
                        self.queue.add_warning(
                            lease,
                            code="index_reconciliation_pending",
                            message=(
                                "The file was published safely but library index reconciliation "
                                "is pending."
                            ),
                        )
                    except Exception:
                        logger.warning(
                            "could not persist post-publication warning: %s",
                            redact(exc),
                            extra={"job_id": lease.job_id},
                        )

            self.queue.complete(
                lease,
                final_relative_path=publication.relative_path,
                final_sha256=publication.sha256,
                final_track_id=final_track_id,
                published=True,
            )
            cleanup_staging_directory(staging)
            return ProcessOutcome(
                job_id=lease.job_id,
                status="completed",
                relative_path=publication.relative_path,
            )
        except InitialLibraryScanPending:
            monitor.stop()
            try:
                status = self.queue.defer_for_library_scan(lease)
            except LeaseLostError:
                status = "lease_lost"
            if status == "cancelled":
                cleanup_staging_directory(staging)
            return ProcessOutcome(job_id=lease.job_id, status=status)
        except (SourceNeedsReview, JobNeedsReview) as exc:
            monitor.stop()
            if isinstance(exc, SourceNeedsReview):
                reason = exc.decision.reason
                options = [
                    {
                        "rank": rank,
                        "kind": "source",
                        "source_id": item.candidate.source_id,
                        "source_extractor": "youtube",
                        "url": item.candidate.url,
                        "title": item.candidate.title,
                        "channel": item.candidate.channel,
                        "duration_seconds": item.candidate.duration_seconds,
                        "score": item.score,
                    }
                    for rank, item in enumerate(exc.decision.ranked[:5], start=1)
                ]
            else:
                reason = exc.reason
                options = exc.options
            review_created = self.queue.require_review(
                lease,
                reason=reason,
                options=options,
                max_rounds_per_category=self.settings.max_review_rounds_per_category,
                max_rounds_per_job=self.settings.max_review_rounds_per_job,
                category=exc.category if isinstance(exc, JobNeedsReview) else None,
            )
            if not self.completed_media or not self.completed_media.has_ready(lease.job_id):
                cleanup_staging_directory(staging)
            status = "needs_review" if review_created else "queued"
            if not review_created and self.session_factory is not None:
                with self.session_factory() as session:
                    status = (
                        session.scalar(
                            select(DownloadJob.status).where(DownloadJob.id == lease.job_id)
                        )
                        or status
                    )
                if status == "failed":
                    cleanup_staging_directory(staging)
            return ProcessOutcome(
                job_id=lease.job_id,
                status=status,
            )
        except DuplicateOwned as exc:
            monitor.stop()
            self.queue.complete(
                lease,
                final_relative_path=exc.track.filepath,
                final_sha256=exc.sha256,
                final_track_id=exc.track.id,
            )
            cleanup_staging_directory(staging)
            return ProcessOutcome(
                job_id=lease.job_id,
                status="completed",
                relative_path=exc.track.filepath,
            )
        except (JobCancellationRequested, DownloadCancelled) as exc:
            monitor.stop()
            if (
                isinstance(exc, DownloadCancelled)
                and self.shutdown_signal is not None
                and self.shutdown_signal.is_set()
            ):
                try:
                    status = self.queue.release_for_shutdown(lease)
                except LeaseLostError:
                    return ProcessOutcome(job_id=lease.job_id, status="lease_lost")
                return ProcessOutcome(job_id=lease.job_id, status=status)
            # DownloadCancelled can also be caused by lease loss/DB failure. Never
            # acknowledge cancellation unless the monitor confirmed that state.
            try:
                monitor.raise_if_unusable()
            except JobCancellationRequested:
                self.queue.acknowledge_cancel(lease)
                cleanup_staging_directory(staging)
                return ProcessOutcome(job_id=lease.job_id, status="cancelled")
            except LeaseLostError:
                return ProcessOutcome(job_id=lease.job_id, status="lease_lost")
            if self.queue.cancellation_requested(lease):
                self.queue.acknowledge_cancel(lease)
                cleanup_staging_directory(staging)
                return ProcessOutcome(job_id=lease.job_id, status="cancelled")
            raise
        except LeaseLostError:
            monitor.stop()
            return ProcessOutcome(job_id=lease.job_id, status="lease_lost")
        except InsufficientSpaceError:
            monitor.stop()
            self.queue.wait_for_space(lease)
            return ProcessOutcome(job_id=lease.job_id, status="waiting_for_space")
        except Exception as exc:
            monitor.stop()
            if publication is not None:
                logger.exception(
                    "published file awaits durable job reconciliation",
                    extra={"job_id": lease.job_id},
                )
                return ProcessOutcome(
                    job_id=lease.job_id,
                    status="published_pending_recovery",
                    relative_path=publication.relative_path,
                )
            retryable = _is_retryable_job_error(exc)
            status = self.queue.fail(
                lease,
                error_code=_error_code(exc),
                error_message=redact(str(exc) or type(exc).__name__),
                retryable=retryable,
            )
            if status == "failed" and (
                not self.completed_media
                or not self.completed_media.has_ready(lease.job_id)
                or isinstance(
                    exc, (MediaBudgetExceeded, SourceValidationError, MediaValidationError)
                )
            ):
                cleanup_staging_directory(staging)
            logger.warning(
                "download job failed (%s): %s",
                status,
                redact(exc),
                extra={"job_id": lease.job_id},
            )
            return ProcessOutcome(job_id=lease.job_id, status=status)
        finally:
            if self.reservations is not None:
                for reservation in (publication_reservation, download_reservation):
                    if reservation is None:
                        continue
                    try:
                        self.reservations.release(reservation)
                    except Exception as exc:
                        logger.warning(
                            "media reservation cleanup will be reconciled: %s",
                            redact(exc),
                            extra={"job_id": lease.job_id},
                        )

    def _resolve_source(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
    ) -> str:
        source_resolver = self.source_resolver if hasattr(self, "source_resolver") else None
        if source_resolver is not None:
            try:
                return source_resolver.resolve(lease, monitor, cancellation).url
            except SourceResolutionNeedsReview as exc:
                raise JobNeedsReview(exc.reason, exc.options) from exc
        snapshot = lease.approved_snapshot
        source_url = _string_or_none(snapshot.get("source_url"))
        if source_url:
            return self.youtube.validate_direct_url(source_url)
        source_id = _string_or_none(snapshot.get("source_id"))
        source_extractor = _string_or_none(snapshot.get("source_extractor"))
        if source_id and source_extractor == "youtube":
            return self.youtube.validate_direct_url(f"https://www.youtube.com/watch?v={source_id}")
        artist = _required_string(snapshot, "artist")
        title = _required_string(snapshot, "title")
        decision = self.youtube.choose(
            TrackIntent(
                artist=artist,
                title=title,
                duration_seconds=_float_or_none(snapshot.get("duration_seconds")),
                version_signature=_string_or_none(snapshot.get("version_signature")) or "studio",
            ),
            cancel_signal=cancellation,
        )
        if decision.selected is None:
            if decision.ambiguous:
                return self._resolve_ambiguous_source(lease, monitor, decision, cancellation)
            raise SourceNeedsReview(decision)
        return decision.selected.url

    def _acquire_valid_source(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
        staging: Path,
        reservation: Reservation | None,
    ) -> tuple[DownloadResult, str, MediaProbe, dict[str, Any]]:
        while True:
            source_url = self._resolve_source(lease, monitor, cancellation)
            monitor.raise_if_unusable()
            try:
                source_metadata = self.ytdlp.probe(source_url, cancel_signal=cancellation)
                self._validate_source_probe(lease, source_url, source_metadata)
                source_values = self._tag_values(
                    lease.approved_snapshot, source_metadata, source_url, job_id=lease.job_id
                )
                duration = _float_or_none(source_metadata.get("duration"))
                tag_values = None
                if duration is not None:
                    if not 0 < duration <= self.settings.max_direct_media_seconds:
                        raise SourceValidationError("source exceeds the configured duration limit")
                    preview_probe = MediaProbe(staging, "", (), duration, None)
                    self._validate_canonical_metadata(source_values, source_metadata, preview_probe)
                    self.queue.set_progress(lease, stage=JobStage.RESOLVING_METADATA, progress=0.06)
                    tag_values = self._resolve_canonical_metadata(
                        source_values,
                        preview_probe,
                        lease=lease,
                        monitor=monitor,
                        cancellation=cancellation,
                        source_metadata=source_metadata,
                    )
                    self._check_duplicate(lease.job_id, tag_values, preview_probe)
                monitor.raise_if_unusable()
                self.queue.set_progress(lease, stage=JobStage.DOWNLOADING, progress=0.08)
                reused = (
                    self.completed_media.find(
                        lease,
                        staging,
                        extractor=_required_string(source_values, "source_extractor"),
                        source_id=_required_string(source_values, "source_id"),
                    )
                    if self.completed_media is not None
                    else None
                )
                if reused is None:
                    if self.completed_media is not None and self.completed_media.has_ready(
                        lease.job_id, include_expired=True
                    ):
                        # A changed, expired, or different-source artifact must not
                        # be mistaken by yt-dlp --no-overwrites for a valid download.
                        self.completed_media.invalidate(lease)
                        if not cleanup_staging_directory(staging):
                            raise RuntimeError("invalid completed media could not be cleaned")
                        create_staging_directory(self.settings.downloads_path, lease.job_id)
                    result = self.ytdlp.download_audio(
                        source_url,
                        staging,
                        max_duration_seconds=self.settings.max_direct_media_seconds,
                        max_media_bytes=self.settings.max_media_bytes,
                        progress_callback=self._progress_callback(lease, monitor),
                        cancel_signal=cancellation,
                    )
                else:
                    result = DownloadResult(
                        path=reused,
                        extractor=_required_string(source_values, "source_extractor"),
                        source_id=_required_string(source_values, "source_id"),
                        metadata=source_metadata,
                    )
                if result.source_id != source_values.get("source_id") or (
                    result.extractor or ""
                ).casefold() != source_values.get("source_extractor"):
                    raise SourceValidationError(
                        "download identity changed after metadata acceptance"
                    )
                if result.path.stat().st_size > self.settings.max_media_bytes:
                    raise MediaBudgetExceeded("downloaded source exceeded the media byte limit")
                monitor.raise_if_unusable()
                media_probe = self._normalize_media(
                    result.path, cancellation, allow_attached_art=reused is not None
                )
                if self.reservations is not None and reservation is not None:
                    self.reservations.update_download(reservation, staging)
                self._validate_canonical_metadata(source_values, result.metadata, media_probe)
                if duration is not None and abs(duration - media_probe.duration_seconds) > max(
                    10.0, duration * 0.05
                ):
                    raise SourceCandidateRejected(
                        "audio duration contradicts the accepted source probe"
                    )
                self._save_completed_media(lease, media_probe.path, staging, source_values)
                if tag_values is None:
                    self.queue.set_progress(lease, stage=JobStage.RESOLVING_METADATA, progress=0.62)
                    tag_values = self._resolve_canonical_metadata(
                        source_values,
                        media_probe,
                        lease=lease,
                        monitor=monitor,
                        cancellation=cancellation,
                        source_metadata=result.metadata,
                    )
                tag_values["duration_seconds"] = media_probe.duration_seconds
                return result, source_url, media_probe, tag_values
            except DownloadCancelled:
                raise
            except (
                YtDlpError,
                SourceValidationError,
                SourceCandidateRejected,
                MediaValidationError,
                MediaBudgetExceeded,
            ) as exc:
                source_resolver = self.source_resolver if hasattr(self, "source_resolver") else None
                if source_resolver is None:
                    raise
                if _is_transient_source_error(exc):
                    # A provider/network outage belongs to the durable job retry
                    # budget, not to the finite candidate-attempt budget.
                    raise
                attempts = source_resolver.reject_active(lease, _error_code(exc))
                if attempts >= self.settings.max_automatic_source_attempts:
                    fallback_review = source_resolver.provider_fallback_review(lease)
                    if fallback_review is not None:
                        reason, options = fallback_review
                        raise JobNeedsReview(reason, options) from exc
                    raise JobNeedsReview(
                        "safe source candidates were exhausted after automatic fallback"
                    ) from exc
                self.queue.add_warning(
                    lease,
                    code="source_candidate_rejected",
                    message="A source failed validation; another safe candidate is being tried.",
                )
                if not cleanup_staging_directory(staging):
                    raise RuntimeError("failed to clean the rejected source staging area") from exc
                create_staging_directory(self.settings.downloads_path, lease.job_id)

    def _save_completed_media(
        self, lease: JobLease, path: Path, staging: Path, values: Mapping[str, Any]
    ) -> None:
        if self.completed_media is not None:
            self.completed_media.save(
                lease,
                path,
                staging,
                extractor=_required_string(dict(values), "source_extractor"),
                source_id=_required_string(dict(values), "source_id"),
            )

    def _validate_source_probe(
        self, lease: JobLease, source_url: str, metadata: Mapping[str, Any]
    ) -> None:
        validate_public_media_metadata(metadata)
        extractor = _first_source_string(metadata, "extractor", "extractor_key")
        source_id = _first_source_string(metadata, "id")
        if (
            metadata.get("entries") is not None
            or metadata.get("_type") in {"playlist", "multi_video"}
            or not source_id
            or not extractor
            or provider_for_extractor(extractor.casefold()) is not provider_for_url(source_url)
        ):
            raise SourceValidationError("source probe did not identify one permitted media item")
        if self.session_factory is not None:
            with self.session_factory() as session:
                job = _leased_job(session, lease, action="validating source probe identity")
                row = (
                    session.get(DbSourceCandidate, job.active_source_candidate_id)
                    if job.active_source_candidate_id
                    else None
                )
                if row is not None and (
                    row.source_id != source_id or row.extractor != extractor.casefold()
                ):
                    raise SourceValidationError(
                        "source probe identity differs from the selected source"
                    )

    def _resolve_ambiguous_source(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        decision: SelectionDecision,
        cancellation: CancellationSignal | None = None,
    ) -> str:
        if cancellation is not None and cancellation.is_set():
            raise DownloadCancelled("source selection was cancelled")
        if self.session_factory is None:
            raise SourceNeedsReview(decision)
        candidates = tuple(item.candidate for item in decision.ranked[:8])
        allowed = {item.source_id: item for item in candidates}
        request_id: str | None = None
        request_track_id = _string_or_none(lease.approved_snapshot.get("request_track_id"))
        if request_track_id is not None:
            with self.session_factory() as session:
                request_id = session.scalar(
                    select(RequestTrack.request_id).where(RequestTrack.id == request_track_id)
                )
        candidate_records = [
            {
                "source_id": candidate.source_id,
                "title": candidate.title,
                "channel": candidate.channel,
                "duration_seconds": candidate.duration_seconds,
            }
            for candidate in candidates
        ]
        payload = {
            "schema_version": 1,
            "request_id": request_id,
            "job_id": lease.job_id,
            "decision_category": "acquisition_source",
            "intent": {
                "artist": _required_string(lease.approved_snapshot, "artist"),
                "title": _required_string(lease.approved_snapshot, "title"),
                "album": _string_or_none(lease.approved_snapshot.get("album")),
                "version": (
                    _string_or_none(lease.approved_snapshot.get("version_signature")) or "studio"
                ),
                "duration_seconds": _float_or_none(lease.approved_snapshot.get("duration_seconds")),
            },
            "candidates": candidate_records,
            "candidate_set_fingerprint": candidate_set_fingerprint(
                "acquisition_source", candidate_records
            ),
        }
        with self.session_factory.begin() as session:
            _leased_job(session, lease, action="requesting legacy source selection")
            task = reuse_or_create_decision_task(
                session,
                target="web",
                kind="select_source",
                payload_version=1,
                payload=payload,
            )
            task_id = task.id
        self.queue.set_progress(lease, stage=JobStage.WAITING_AI, progress=0.04)
        deadline = time.monotonic() + float(self.settings.max_agent_seconds + 5)
        while time.monotonic() < deadline:
            if cancellation is not None and cancellation.is_set():
                raise DownloadCancelled("source selection was cancelled")
            monitor.raise_if_unusable()
            with self.session_factory() as session:
                row = session.get(ServiceTask, task_id)
                if row is None:
                    return self._source_selection_review(
                        decision, "the source selector task disappeared"
                    )
                state, result_json, last_error = row.state, row.result_json, row.last_error
            if state == "completed":
                try:
                    result = json.loads(result_json or "null")
                except json.JSONDecodeError:
                    return self._source_selection_review(
                        decision, "the source selector returned malformed output"
                    )
                if not isinstance(result, dict):
                    return self._source_selection_review(
                        decision, "the source selector returned invalid output"
                    )
                selected_id = result.get("selected_source_id")
                needs_review = result.get("needs_review")
                if selected_id is None or needs_review is True:
                    reason = _string_or_none(result.get("rationale")) or decision.reason
                    return self._source_selection_review(decision, reason)
                if not isinstance(selected_id, str) or selected_id not in allowed:
                    return self._source_selection_review(
                        decision, "the source selector returned an unknown candidate ID"
                    )
                return allowed[selected_id].url
            if state == "failed":
                return self._source_selection_review(
                    decision,
                    f"the source selector failed: {_string_or_none(last_error) or 'unavailable'}",
                )
            time.sleep(0.2)
        return self._source_selection_review(decision, "the source selector timed out")

    @staticmethod
    def _source_selection_review(decision: SelectionDecision, reason: str) -> str:
        raise SourceNeedsReview(
            replace(decision, selected=None, needs_review=True, reason=reason, ambiguous=False)
        )

    def _normalize_media(
        self,
        path: Path,
        cancellation: CancellationSignal,
        *,
        allow_attached_art: bool = False,
    ) -> MediaProbe:
        if self.media is None:
            raise MediaValidationError("ffmpeg/ffprobe media verification is not configured")
        return self.media.normalize_and_verify(
            path,
            max_duration_seconds=self.settings.max_direct_media_seconds,
            allow_lossy_transcode=self.settings.allow_lossy_transcode,
            cancel_signal=cancellation,
            allow_attached_art=allow_attached_art,
        )

    def _validate_canonical_metadata(
        self,
        expected: dict[str, Any],
        source: Mapping[str, Any],
        probe: MediaProbe,
    ) -> None:
        expected_artist = _required_string(expected, "artist")
        expected_title = _required_string(expected, "title")
        expected_version = DEFAULT_VERSION_CLASSIFIER.classify(
            expected_title,
            _explicit_version_constraint(expected),
        )
        source_version = DEFAULT_VERSION_CLASSIFIER.classify(
            _first_source_string(source, "title"),
            _first_source_string(source, "track"),
            _first_source_string(source, "alt_title"),
            _first_source_string(source, "version"),
        )
        if DEFAULT_VERSION_CLASSIFIER.contradictions(expected_version, source_version):
            raise SourceCandidateRejected(
                "the downloaded source is a contradictory recording version"
            )
        recording = resolve_provider_recording_metadata(source)
        provider_artist = recording.artist
        provider_title = recording.title
        if provider_title is None:
            raise SourceCandidateRejected(
                "the downloaded source did not expose enough track metadata to match"
            )
        if provider_artist is None:
            # Uploader identity is deliberately excluded. An exact title plus the
            # already-probed duration can still corroborate the canonical artist.
            provider_artist = expected_artist
        if provider_artist.casefold().endswith(" - topic"):
            provider_artist = provider_artist[:-8].strip()
        provider_title = strip_provider_suffixes(provider_title)
        for prefix_artist in (provider_artist, expected_artist):
            prefix = f"{prefix_artist} - "
            if provider_title.casefold().startswith(prefix.casefold()):
                provider_title = provider_title[len(prefix) :].strip()
                break
        candidate = MetadataCandidate(
            artist=provider_artist,
            artists=recording.artists,
            title=provider_title,
            album=_first_source_string(source, "album"),
            duration_seconds=probe.duration_seconds,
            source=_first_source_string(source, "extractor", "extractor_key") or "media",
            raw=source,
        )
        ranked = self.metadata_matcher.rank(
            artist=expected_artist,
            artists=structured_artists(expected.get("artists")),
            title=expected_title,
            album=_explicit_album_constraint(expected),
            duration_seconds=_float_or_none(expected.get("duration_seconds")),
            requested_version=_explicit_version_constraint(expected),
            version_is_explicit=_version_constraint_explicit(expected),
            candidates=[candidate],
            limit=1,
        )
        match = ranked[0]
        if match.decision != "auto":
            raise SourceCandidateRejected(
                "downloaded source metadata does not confidently match the approved track",
                [
                    {
                        "kind": "metadata",
                        "rank": 1,
                        "artist": provider_artist,
                        "title": provider_title,
                        "album": candidate.album,
                        "duration_seconds": probe.duration_seconds,
                        "score": match.score,
                        "reasons": list(match.reasons),
                    }
                ],
            )

    def _check_duplicate(self, job_id: str, values: dict[str, Any], probe: MediaProbe) -> None:
        if self.session_factory is None:
            raise RuntimeError("duplicate checking is not configured")
        candidate = DuplicateCandidate(
            artist=_required_string(values, "artist"),
            title=_required_string(values, "title"),
            version_signature=_string_or_none(values.get("version_signature")) or "studio",
            duration_seconds=probe.duration_seconds,
            recording_mbid=_string_or_none(values.get("recording_mbid")),
            source_extractor=_string_or_none(values.get("source_extractor")),
            source_id=_string_or_none(values.get("source_id")),
        )
        with self.session_factory.begin() as session:
            decision = self.duplicate_detector.find(session, candidate)
            track = session.get(Track, decision.track_id) if decision.track_id else None
            options = _possible_duplicate_options(track, decision.reason)
            fingerprint = candidate_set_fingerprint("possible_duplicate", options)
            reviewed_duplicate = selected_payload(
                session,
                job_id,
                "possible_duplicate",
                fingerprint,
            )
        if decision.status == "possible" and reviewed_duplicate is None:
            raise JobNeedsReview(
                decision.reason or "a possible duplicate requires review",
                options,
            )
        if decision.status == "possible" and reviewed_duplicate is not None:
            action = _string_or_none(reviewed_duplicate.get("duplicate_action"))
            selected_track_id = _string_or_none(reviewed_duplicate.get("track_id"))
            if action == "import_separate" and selected_track_id == decision.track_id:
                return
            if action == "use_existing" and track is not None and selected_track_id == track.id:
                existing = self.settings.music_path / track.filepath
                digest = track.file_sha256 or sha256_file(existing)
                raise DuplicateOwned(track, digest)
            # Legacy or mismatched decisions do not authorize suppressing a new
            # possible-duplicate warning.
            raise JobNeedsReview(
                decision.reason or "a possible duplicate requires review",
                options,
            )
        if decision.status == "owned" and track is not None:
            existing = self.settings.music_path / track.filepath
            digest = track.file_sha256 or sha256_file(existing)
            raise DuplicateOwned(track, digest)

    def _resolve_canonical_metadata(
        self,
        values: dict[str, Any],
        probe: MediaProbe,
        *,
        lease: JobLease | None = None,
        monitor: LeaseMonitor | None = None,
        cancellation: CancellationSignal | None = None,
        source_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        durable_replay = None
        if lease is not None and self.session_factory is not None:
            with self.session_factory() as session:
                durable_replay = latest_canonical_selection(
                    session,
                    lease.job_id,
                    source_extractor=_string_or_none(values.get("source_extractor")),
                    source_id=_string_or_none(values.get("source_id")),
                )
        if durable_replay is not None:
            if durable_replay.payload.get("metadata_authority") in FALLBACK_AUTHORITIES:
                return self._apply_provider_metadata(values, durable_replay.payload, lease)
            candidate = _metadata_candidate_from_payload(durable_replay.payload)
            authority = durable_replay.decided_by
            model_confidence = durable_replay.model_confidence
            openai_call_id = durable_replay.openai_call_id
            options: list[dict[str, Any]] = []
        else:
            if self.metadata_resolver is None:
                raise RuntimeError("MusicBrainz canonical metadata resolution is not configured")
            try:
                resolution = self.metadata_resolver.resolve(
                    artist=_required_string(values, "artist"),
                    artists=structured_artists(values.get("artists")),
                    title=_required_string(values, "title"),
                    album=_explicit_album_constraint(values),
                    duration_seconds=probe.duration_seconds,
                    version_signature=_explicit_version_constraint(values),
                    album_is_explicit=_album_constraint_explicit(values),
                    year=_int_or_none(values.get("year")),
                )
            except WorkerMetadataError as exc:
                if self.settings.canonical_metadata_policy == "require" and exc.retryable:
                    raise
                return self._fallback_or_review(
                    values,
                    probe,
                    source_metadata,
                    lease,
                    options=[],
                    reason_code=exc.reason_code,
                )
            if monitor is not None:
                monitor.raise_if_unusable()
            options = list(resolution.options)
            fingerprint = candidate_set_fingerprint("canonical_metadata", options)
            replayed: dict[str, object] | None = None
            replayed_authority: str | None = None
            replayed_model_confidence: float | None = None
            replayed_openai_call_id: str | None = None
            if lease is not None and self.session_factory is not None:
                with self.session_factory() as session:
                    replayed = selected_payload(
                        session, lease.job_id, "canonical_metadata", fingerprint
                    )
                    prior_decision = selected_decision(
                        session, lease.job_id, "canonical_metadata", fingerprint
                    )
                    if prior_decision is not None:
                        replayed_authority = prior_decision.decided_by
                        replayed_model_confidence = prior_decision.model_confidence
                        replayed_openai_call_id = prior_decision.openai_call_id
            authority = "deterministic"
            model_confidence = None
            openai_call_id = None
            if replayed is not None:
                candidate = _metadata_candidate_from_payload(replayed)
                authority = replayed_authority or "deterministic"
                model_confidence = replayed_model_confidence
                openai_call_id = replayed_openai_call_id
            elif resolution.decision == "auto" and resolution.candidate is not None:
                candidate = resolution.candidate
            elif (
                resolution.decision == "review"
                and options
                and lease is not None
                and monitor is not None
                and cancellation is not None
                and self.session_factory is not None
                and getattr(self.settings, "ai_match_resolution_enabled", False)
            ):
                model_result = self._ask_openai_canonical(
                    lease,
                    monitor,
                    cancellation,
                    values,
                    options,
                )
                selected, decision = self._adjudicate_canonical_model(
                    values, probe, options, model_result
                )
                if selected is None or decision is None:
                    return self._fallback_or_review(
                        values,
                        probe,
                        source_metadata,
                        lease,
                        options=options,
                        reason_code="low_confidence",
                    )
                candidate = _metadata_candidate_from_payload(selected)
                authority = "openai"
                model_confidence = decision.confidence
                openai_call_id = _string_or_none(model_result.get("openai_call_id"))
            else:
                return self._fallback_or_review(
                    values,
                    probe,
                    source_metadata,
                    lease,
                    options=options if resolution.decision == "review" else [],
                    reason_code=resolution.reason_code,
                )
        if durable_replay is None and lease is not None and self.session_factory is not None:
            selected_option = next(
                (
                    option
                    for option in options
                    if option.get("recording_mbid") == candidate.recording_mbid
                    and option.get("release_mbid") == candidate.release_mbid
                ),
                _metadata_payload(candidate),
            )
            local_confidence = _float_or_none(selected_option.get("local_score"))
            with self.session_factory.begin() as session:
                job = _leased_job(session, lease, action="persisting canonical metadata")
                record_selected_decision(
                    session,
                    job,
                    category="canonical_metadata",
                    candidates=options,
                    selected_payload={
                        **_metadata_payload(candidate),
                        "source_extractor": values.get("source_extractor"),
                        "source_id": values.get("source_id"),
                        "metadata_authority": "musicbrainz",
                        "canonical_identity_verified": candidate.recording_mbid is not None,
                        "recording_candidate_id": selected_option.get("recording_candidate_id"),
                        "release_candidate_id": selected_option.get("release_candidate_id"),
                    },
                    decided_by=authority,
                    reason_codes=["canonical_match_accepted"],
                    local_confidence=local_confidence,
                    model_confidence=model_confidence,
                    openai_call_id=openai_call_id,
                    prompt_version=("canonical_matcher_v2" if authority == "openai" else None),
                )
        enriched = dict(values)
        enriched["artist"] = candidate.artist
        enriched["artists"] = list(candidate.artists or (candidate.artist,))
        enriched["title"] = candidate.title
        if candidate.album:
            enriched["album"] = candidate.album
        enriched["album_artist"] = candidate.artist
        enriched["album_artists"] = [candidate.artist]
        if candidate.year is not None:
            enriched["year"] = candidate.year
        for key in ("recording_mbid", "release_mbid", "release_group_mbid"):
            # Always overwrite these fields so an unverified model suggestion can
            # never survive a later local MusicBrainz resolution by omission.
            enriched[key] = getattr(candidate, key)
        enriched["canonical_identity_verified"] = candidate.recording_mbid is not None
        enriched["metadata_authority"] = (
            "musicbrainz" if candidate.recording_mbid else "user_confirmed_provider_metadata"
        )
        raw_provenance = values.get("metadata_provenance")
        provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else {}
        raw_resolution = provenance.get("canonical_metadata_resolution")
        canonical_resolution = dict(raw_resolution) if isinstance(raw_resolution, Mapping) else {}
        canonical_resolution.update(
            {
                "source": (
                    "user_confirmed_server_candidate"
                    if authority == "user"
                    else "musicbrainz_local_candidate"
                ),
                "automatic_association": authority != "user",
                "decided_by": authority,
            }
        )
        provenance["canonical_metadata_resolution"] = canonical_resolution
        enriched["metadata_provenance"] = provenance
        return enriched

    def _source_authority(
        self, lease: JobLease | None, values: Mapping[str, Any]
    ) -> SourceAuthority:
        if lease is None or self.session_factory is None:
            return SourceAuthority()
        with self.session_factory() as session:
            job = _leased_job(session, lease, action="checking source metadata authority")
            row = (
                session.get(DbSourceCandidate, job.active_source_candidate_id)
                if job.active_source_candidate_id
                else None
            )
            if (
                row is None
                or row.job_id != lease.job_id
                or row.policy_status != "allowed"
                or row.probe_status != "valid"
                or row.failure_code is not None
                or row.source_id != values.get("source_id")
                or row.extractor != values.get("source_extractor")
            ):
                return SourceAuthority()
            evidence = session.get(EvidenceReference, row.evidence_id) if row.evidence_id else None
            track = session.get(RequestTrack, job.request_track_id)
            contradictions = json.loads(row.contradictions_json or "[]")
            return SourceAuthority(
                validated=True,
                direct_approved=bool(
                    evidence is not None
                    and evidence.evidence_kind == "direct_user_url"
                    and evidence.request_track_id == job.request_track_id
                    and track is not None
                    and track.approved_at is not None
                ),
                local_score=row.local_score,
                contradictions=tuple(str(item) for item in contradictions),
            )

    def _fallback_or_review(
        self,
        values: dict[str, Any],
        probe: MediaProbe,
        source_metadata: Mapping[str, Any] | None,
        lease: JobLease | None,
        *,
        options: list[dict[str, Any]],
        reason_code: str,
    ) -> dict[str, Any]:
        fallback = provider_fallback(
            values,
            source_metadata or {},
            authority=self._source_authority(lease, values),
            duration=probe.duration_seconds,
            minimum_score=getattr(self.settings, "provider_metadata_fallback_min_score", 0.90),
        )
        if fallback is not None:
            payload = {**fallback.payload, "reason_code": reason_code}
            if self.settings.canonical_metadata_policy == "prefer" and fallback.automatic:
                assert lease is not None and self.session_factory is not None
                with self.session_factory.begin() as session:
                    job = _leased_job(
                        session, lease, action="accepting validated provider metadata"
                    )
                    record_selected_decision(
                        session,
                        job,
                        category="canonical_metadata",
                        candidates=[payload],
                        selected_payload=payload,
                        decided_by="deterministic",
                        reason_codes=["provider_metadata_fallback", reason_code],
                        local_confidence=_float_or_none(fallback.payload["local_score"]),
                    )
                return self._apply_provider_metadata(values, payload, lease)
            options = [payload, *options[:7]]
        reason = (
            "No confident MusicBrainz match was found. You may use the validated source "
            "metadata or correct it manually."
            if fallback is not None
            else "MusicBrainz metadata could not be confirmed. Review the recording metadata."
        )
        if reason_code == "malformed_response":
            reason = "MusicBrainz returned invalid metadata. Review the recording metadata."
        raise JobNeedsReview(reason, options, category="canonical_metadata")

    def _apply_provider_metadata(
        self,
        values: dict[str, Any],
        payload: Mapping[str, Any],
        lease: JobLease | None,
    ) -> dict[str, Any]:
        enriched = dict(values)
        for key in (
            "artist",
            "artists",
            "title",
            "album",
            "album_artist",
            "year",
            "duration_seconds",
            "metadata_authority",
        ):
            if key in payload:
                enriched[key] = payload[key]
        enriched["album_artists"] = [enriched.get("album_artist") or enriched["artist"]]
        for key in ("recording_mbid", "release_mbid", "release_group_mbid"):
            enriched[key] = None
            enriched.pop(f"suggested_{key}", None)
        enriched["canonical_identity_verified"] = False
        authority = str(payload["metadata_authority"])
        provenance = dict(values.get("metadata_provenance") or {})
        provenance["canonical_metadata_resolution"] = {
            "source": authority,
            "automatic_association": authority != "user_confirmed_provider_metadata",
            "decided_by": "user"
            if authority == "user_confirmed_provider_metadata"
            else "deterministic",
            "reason_code": payload.get("reason_code") or "user_selected_provider_metadata",
        }
        enriched["metadata_provenance"] = provenance
        if lease is not None:
            self.queue.add_warning(
                lease, code="provider_metadata_fallback", message=FALLBACK_WARNING
            )
        return enriched

    def _ask_openai_canonical(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
        values: Mapping[str, Any],
        options: list[dict[str, Any]],
    ) -> dict[str, object]:
        request_track_id = _string_or_none(lease.approved_snapshot.get("request_track_id"))
        request_id: str | None = None
        if request_track_id is not None and self.session_factory is not None:
            with self.session_factory() as session:
                request_id = session.scalar(
                    select(RequestTrack.request_id).where(RequestTrack.id == request_track_id)
                )
        recording_candidates = [
            {
                "recording_candidate_id": option.get("recording_candidate_id"),
                "release_candidate_id": option.get("release_candidate_id"),
                "artist": option.get("artist"),
                "title": option.get("title"),
                "album": option.get("album"),
                "year": option.get("year"),
                "duration_seconds": option.get("duration_seconds"),
                "local_score": option.get("local_score"),
                "version": option.get("version"),
                "reason_codes": option.get("reason_codes", []),
            }
            for option in options[:8]
        ]
        release_candidates = [
            {
                "release_candidate_id": option.get("release_candidate_id"),
                "recording_candidate_id": option.get("recording_candidate_id"),
                "album": option.get("album"),
                "year": option.get("year"),
                "status": option.get("release_status"),
                "primary_type": option.get("primary_type"),
                "local_score": option.get("local_score"),
            }
            for option in options[:8]
            if option.get("release_candidate_id") is not None
        ]
        payload = {
            "schema_version": 2,
            "request_id": request_id,
            "job_id": lease.job_id,
            "decision_category": "canonical_metadata",
            "intent": {
                "artist": _required_string(dict(values), "artist"),
                "title": _required_string(dict(values), "title"),
                "album": _explicit_album_constraint(values),
                "version": _explicit_version_constraint(values),
                "duration_seconds": _float_or_none(values.get("duration_seconds")),
            },
            "recording_candidates": recording_candidates,
            "release_candidates": release_candidates,
        }
        payload["candidate_set_fingerprint"] = candidate_set_fingerprint(
            "canonical_metadata", options[:8]
        )
        assert self.session_factory is not None
        with self.session_factory.begin() as session:
            _leased_job(session, lease, action="requesting canonical metadata selection")
            task = reuse_or_create_decision_task(
                session,
                target="web",
                kind="match_canonical",
                payload_version=2,
                payload=payload,
            )
            task_id = task.id
        self.queue.set_progress(lease, stage=JobStage.WAITING_AI, progress=0.64)
        deadline = time.monotonic() + float(self.settings.max_agent_seconds + 5)
        while time.monotonic() < deadline:
            if cancellation.is_set():
                raise DownloadCancelled("canonical metadata selection was cancelled")
            monitor.raise_if_unusable()
            with self.session_factory() as session:
                row = session.get(ServiceTask, task_id)
                if row is None or row.state == "failed":
                    return {}
                if row.state == "completed":
                    try:
                        result = json.loads(row.result_json or "{}")
                    except json.JSONDecodeError:
                        return {}
                    return result if isinstance(result, dict) else {}
            time.sleep(0.2)
        return {}

    def _adjudicate_canonical_model(
        self,
        values: Mapping[str, Any],
        probe: MediaProbe,
        options: list[dict[str, Any]],
        model_result: Mapping[str, object],
    ) -> tuple[dict[str, Any] | None, CanonicalMatchDecision | None]:
        recording_ids = [
            identifier
            for option in options
            if isinstance((identifier := option.get("recording_candidate_id")), str)
        ]
        release_ids = [
            identifier
            for option in options
            if isinstance((identifier := option.get("release_candidate_id")), str)
        ]
        raw_decision = model_result.get("decision")
        if not isinstance(raw_decision, Mapping):
            return None, None
        try:
            decision = validate_canonical_match_decision(
                raw_decision,
                recording_candidate_ids=recording_ids,
                release_candidate_ids=release_ids,
            )
        except (TypeError, ValueError):
            return None, None
        if (
            decision.decision is not MatchDecision.MATCH
            or decision.confidence < self.settings.ai_match_auto_accept_threshold
            or decision.contradiction_codes
        ):
            return None, decision
        selected = next(
            (
                option
                for option in options
                if option.get("recording_candidate_id") == decision.selected_recording_candidate_id
            ),
            None,
        )
        if selected is None:
            return None, decision
        selected_release = decision.selected_release_candidate_id
        if (
            selected_release is not None
            and selected.get("release_candidate_id") != selected_release
        ):
            return None, decision
        local_score = _float_or_none(selected.get("local_score")) or 0.0
        if local_score < self.settings.ai_match_min_local_score:
            return None, decision
        option_contradictions = selected.get("contradiction_codes")
        if isinstance(option_contradictions, list) and option_contradictions:
            return None, decision
        expected_version = _explicit_version_constraint(values)
        if expected_version is None:
            expected_version = self.settings.default_version_preference
        candidate_version = _string_or_none(selected.get("version")) or "studio"
        if not _metadata_versions_compatible(expected_version, candidate_version):
            return None, decision
        if not _metadata_versions_compatible(expected_version, decision.recording_version):
            return None, decision
        expected_album = _explicit_album_constraint(values)
        if expected_album is not None:
            candidate_album = _string_or_none(selected.get("album"))
            if candidate_album is None or normalize_metadata_text(
                expected_album
            ) != normalize_metadata_text(candidate_album):
                return None, decision
        expected_artist = _required_string(dict(values), "artist")
        expected_title = _required_string(dict(values), "title")
        artist = _string_or_none(selected.get("artist")) or ""
        title = _string_or_none(selected.get("title")) or ""
        if (
            token_set_ratio(expected_artist, artist) < 80
            or token_set_ratio(expected_title, title) < 80
        ):
            return None, decision
        duration = _float_or_none(selected.get("duration_seconds"))
        if duration is not None:
            tolerance = max(10.0, probe.duration_seconds * 0.05)
            if abs(duration - probe.duration_seconds) > tolerance:
                return None, decision
        return selected, decision

    def _fetch_artwork(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        source_metadata: Mapping[str, Any],
        tag_values: Mapping[str, Any],
    ) -> Artwork | None:
        if self.artwork_fetcher is None:
            return None
        archive_urls = cover_art_archive_urls(
            tag_values.get("release_mbid"),
            tag_values.get("release_group_mbid"),
        )
        thumbnail = youtube_thumbnail_url(dict(source_metadata))
        candidates = [*archive_urls]
        if thumbnail is not None:
            candidates.append(thumbnail)
        if not candidates:
            return None
        self.queue.set_progress(lease, stage=JobStage.FETCHING_ARTWORK, progress=0.69)
        for index, artwork_url in enumerate(candidates):
            monitor.raise_if_unusable()
            is_thumbnail = index == len(archive_urls) and thumbnail is not None
            try:
                artwork = self.artwork_fetcher.fetch(artwork_url)
            except Exception as exc:
                logger.warning(
                    "optional artwork candidate failed: %s",
                    redact(exc),
                    extra={"job_id": lease.job_id},
                )
                continue
            if is_thumbnail:
                message = (
                    "Cover Art Archive artwork was unavailable; a validated YouTube "
                    "thumbnail was embedded."
                )
                try:
                    self.queue.add_warning(lease, code="youtube_thumbnail_artwork", message=message)
                except Exception as exc:
                    logger.warning(
                        "optional artwork warning could not be persisted: %s",
                        redact(exc),
                        extra={"job_id": lease.job_id},
                    )
                logger.warning(message, extra={"job_id": lease.job_id})
            return artwork
        if candidates:
            logger.warning(
                "no safe artwork was available",
                extra={"job_id": lease.job_id},
            )
        return None

    def _tag_values(
        self,
        snapshot: dict[str, Any],
        source_metadata: Mapping[str, Any],
        source_url: str,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        values = dict(snapshot)
        values["artist"] = _required_string(snapshot, "artist")
        values["title"] = _required_string(snapshot, "title")
        values["source_url"] = source_url
        values["source_extractor"] = (
            _string_or_none(source_metadata.get("extractor"))
            or _string_or_none(source_metadata.get("extractor_key"))
            or _string_or_none(snapshot.get("source_extractor"))
        )
        if isinstance(values["source_extractor"], str):
            values["source_extractor"] = values["source_extractor"].casefold()
        values["source_id"] = _string_or_none(source_metadata.get("id"))
        provider = provider_for_url(source_url)
        values["source_provider"] = provider.value if provider is not None else None
        recording = resolve_provider_recording_metadata(source_metadata)
        values["source_uploader"] = recording.uploader
        provenance = snapshot.get("metadata_provenance")
        approved_artists = structured_artists(snapshot.get("artists"))
        if not approved_artists and isinstance(provenance, Mapping):
            approved_artists = structured_artists(provenance.get("artists"))
        values["artists"] = list(approved_artists or (values["artist"],))
        values["job_id"] = job_id
        return values

    def _progress_callback(self, lease: JobLease, monitor: LeaseMonitor) -> Any:
        lock = threading.Lock()
        last_update = 0.0
        last_progress = 0.08

        def callback(progress: DownloadProgress) -> None:
            nonlocal last_update, last_progress
            monitor.raise_if_unusable()
            if (
                progress.downloaded_bytes is not None
                and progress.downloaded_bytes > self.settings.max_media_bytes
            ):
                raise MediaBudgetExceeded("download exceeded the configured media byte limit")
            fraction = (progress.percent or 0.0) / 100.0
            mapped = 0.08 + min(1.0, max(0.0, fraction)) * 0.52
            now = time.monotonic()
            with lock:
                if now - last_update < 1.0 and mapped - last_progress < 0.02:
                    return
                self.queue.set_progress(lease, stage=JobStage.DOWNLOADING, progress=mapped)
                last_update = now
                last_progress = mapped

        return callback

    def _publish_or_adopt(
        self, source: Path, relative_path: str, *, source_id: str | None
    ) -> PublicationResult:
        self._ensure_library_ready()
        try:
            return self._publish_candidate(source, relative_path)
        except _FileContentCollision as exc:
            if source_id is None:
                raise PublicationConflict(
                    "library destination collides and no stable source ID is available"
                ) from exc
            collision_path = add_source_collision_suffix(relative_path, source_id)
            try:
                return self._publish_candidate(source, collision_path)
            except _FileContentCollision as collision_exc:
                raise PublicationConflict(
                    "source-specific library destination already has different content"
                ) from collision_exc

    def _ensure_library_ready(self) -> None:
        if self.settings.initial_scan_required and (
            self.session_factory is None
            or not LibraryRepository(self.session_factory).initial_scan_complete()
        ):
            raise InitialLibraryScanPending("waiting for the initial library scan")

    def _publish_candidate(self, source: Path, relative_path: str) -> PublicationResult:
        try:
            return publish_no_clobber(
                source,
                self.settings.music_path,
                relative_path,
                reserve_bytes=self.settings.min_free_bytes,
                remove_source=True,
            )
        except DestinationExistsError as exc:
            destination = self.settings.music_path.resolve(strict=True) / relative_path
            if destination.is_symlink() or not destination.is_file():
                raise PublicationConflict(
                    "library destination exists but is not a regular file"
                ) from exc
            source_hash = sha256_file(source)
            destination_hash = sha256_file(destination)
            if source_hash != destination_hash:
                raise _FileContentCollision(relative_path) from exc
            size = destination.stat().st_size
            source.unlink()
            return PublicationResult(
                path=destination,
                relative_path=relative_path,
                sha256=destination_hash,
                size=size,
            )

    def _index_publication(self, publication: PublicationResult) -> str | None:
        if self.library_scanner is None or self.session_factory is None:
            raise RuntimeError("post-publication library indexing is not configured")
        indexed = self.library_scanner.index_one(publication.path)
        with self.session_factory.begin() as session:
            track = session.get(Track, indexed.id)
            if track is None:
                raise RuntimeError("indexed track disappeared before publication commit")
            track.file_sha256 = publication.sha256
        return indexed.id

    def _publish_cover_sidecar(
        self, artwork: Artwork | None, publication: PublicationResult
    ) -> None:
        if artwork is None:
            return
        album_directory = PurePosixPath(publication.relative_path).parent
        try:
            publish_album_cover_no_clobber(
                artwork_as_jpeg(artwork),
                self.settings.music_path,
                album_directory,
                reserve_bytes=self.settings.min_free_bytes,
            )
        except DestinationExistsError:
            # Existing sidecars belong to the user/library and are never replaced.
            return
        except (ArtworkError, OSError, ValueError) as exc:
            logger.warning("optional cover.jpg publication failed: %s", redact(exc))


def _required_string(value: dict[str, Any], key: str) -> str:
    result = _string_or_none(value.get(key))
    if result is None:
        raise ValueError(f"approved snapshot is missing {key}")
    return result


class _CombinedCancellation:
    def __init__(self, *signals: CancellationSignal | None) -> None:
        self.signals = tuple(signal for signal in signals if signal is not None)

    def is_set(self) -> bool:
        return any(signal.is_set() for signal in self.signals)


def _first_source_string(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        result = _string_or_none(value.get(key))
        if result is not None:
            return result
    return None


def _metadata_payload(candidate: MetadataCandidate) -> dict[str, object]:
    return {
        "artist": candidate.artist,
        "artists": list(candidate.artists or (candidate.artist,)),
        "title": candidate.title,
        "album": candidate.album,
        "year": candidate.year,
        "duration_seconds": candidate.duration_seconds,
        "recording_mbid": candidate.recording_mbid,
        "release_mbid": candidate.release_mbid,
        "release_group_mbid": candidate.release_group_mbid,
        "version": candidate.version,
    }


def _metadata_candidate_from_payload(value: Mapping[str, object]) -> MetadataCandidate:
    artist = _string_or_none(value.get("artist"))
    title = _string_or_none(value.get("title"))
    if artist is None or title is None:
        raise JobNeedsReview("the persisted canonical metadata decision is incomplete")
    return MetadataCandidate(
        artist=artist,
        artists=structured_artists(value.get("artists")),
        title=title,
        album=_string_or_none(value.get("album")),
        year=_int_or_none(value.get("year")),
        duration_seconds=_float_or_none(value.get("duration_seconds")),
        recording_mbid=_string_or_none(value.get("recording_mbid")),
        release_mbid=_string_or_none(value.get("release_mbid")),
        release_group_mbid=_string_or_none(value.get("release_group_mbid")),
        source="persisted_canonical_decision",
    )


def _possible_duplicate_options(track: Track | None, reason: str | None) -> list[dict[str, object]]:
    if track is None:
        return []
    shared = {
        "kind": "possible_duplicate",
        "track_id": track.id,
        "artist": track.artist,
        "title": track.title,
        "album": track.album,
        "duration_seconds": track.duration_seconds,
        "reason": reason or "similar library track",
        "materially_different": True,
    }
    return [
        {
            **shared,
            "rank": 1,
            "duplicate_action": "use_existing",
            "label": "Use the existing library copy",
            "score": 1.0,
        },
        {
            **shared,
            "rank": 2,
            "duplicate_action": "import_separate",
            "label": "Import this as a separate track",
            "score": 0.5,
        },
    ]


def _string_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _is_retryable_job_error(exc: Exception) -> bool:
    if isinstance(exc, WorkerMetadataError):
        return exc.retryable
    if isinstance(exc, (YtDlpError, SourceValidationError)):
        return _is_transient_source_error(exc)
    return isinstance(exc, (WorkerMetadataError, OSError, ArtworkError)) and not isinstance(
        exc,
        (
            TaggingError,
            UnsupportedMediaFormat,
            PublicationConflict,
            MediaValidationError,
        ),
    )


def _album_constraint_explicit(values: Mapping[str, object]) -> bool:
    return _constraint_explicit(values, "album")


def _version_constraint_explicit(values: Mapping[str, object]) -> bool:
    return _constraint_explicit(values, "version")


def _constraint_explicit(values: Mapping[str, object], name: str) -> bool:
    flag = f"{name}_constraint_explicit"
    provenance = values.get("metadata_provenance")
    if isinstance(provenance, Mapping):
        user_constraints = provenance.get("user_constraints")
        if isinstance(user_constraints, Mapping) and isinstance(user_constraints.get(flag), bool):
            return user_constraints[flag] is True
    if values.get(flag) is True:
        return True
    if isinstance(provenance, Mapping):
        if provenance.get(flag) is True:
            return True
        request_constraints = provenance.get("request_constraints")
        return isinstance(request_constraints, Mapping) and request_constraints.get(flag) is True
    return False


def _explicit_album_constraint(values: Mapping[str, object]) -> str | None:
    if not _album_constraint_explicit(values):
        return None
    provenance = values.get("metadata_provenance")
    if isinstance(provenance, Mapping):
        user_constraints = provenance.get("user_constraints")
        if (
            isinstance(user_constraints, Mapping)
            and user_constraints.get("album_constraint_explicit") is True
        ):
            return _string_or_none(user_constraints.get("requested_album"))
    return _string_or_none(values.get("requested_album")) or _string_or_none(values.get("album"))


def _explicit_version_constraint(values: Mapping[str, object]) -> str | None:
    if not _version_constraint_explicit(values):
        return None
    return _string_or_none(values.get("requested_version")) or _string_or_none(
        values.get("version_signature")
    )


def _metadata_versions_compatible(left: str | None, right: str | None) -> bool:
    def parts(value: str | None) -> frozenset[str]:
        normalized = (value or "studio").casefold().replace("_", " ").replace("-", " ")
        return frozenset(
            " ".join(part.split()) for part in re.split(r"[|+]", normalized) if part.strip()
        ) or frozenset({"studio"})

    return parts(left) == parts(right)


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


def _error_code(exc: Exception) -> str:
    if isinstance(exc, WorkerMetadataError):
        reason = (
            exc.reason_code
            if exc.reason_code
            in {"temporary_failure", "malformed_response", "rejected_request", "not_found"}
            else "temporary_failure"
        )
        return f"musicbrainz_{reason}"
    name = type(exc).__name__
    result: list[str] = []
    for character in name:
        if character.isupper() and result:
            result.append("_")
        result.append(character.casefold())
    return "".join(result)[:80]
