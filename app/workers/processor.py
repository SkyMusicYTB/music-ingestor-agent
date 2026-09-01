from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
    DownloadProgress,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
)
from app.config import Settings
from app.db.enums import JobStage
from app.db.models import JobReviewOption, RequestTrack, ServiceTask, Track
from app.logging import redact
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
from app.services.metadata_matching import MetadataCandidate, MetadataMatcher
from app.services.source_selection import SelectionDecision, TrackIntent
from app.tags import TaggingError, UnsupportedMediaFormat, write_tags
from app.tools.youtube import YouTubeTool
from app.workers.cleanup import cleanup_staging_directory
from app.workers.lease import LeaseMonitor
from app.workers.media import MediaProbe, MediaProcessor, MediaValidationError
from app.workers.metadata import MusicBrainzWorkerResolver, WorkerMetadataError
from app.workers.queue import (
    DownloadJobQueue,
    JobCancellationRequested,
    JobLease,
    LeaseLostError,
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
    def __init__(self, reason: str, options: list[dict[str, Any]] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.options = options or []


class DuplicateOwned(RuntimeError):
    def __init__(self, track: Track, sha256: str) -> None:
        super().__init__("the requested track is already present")
        self.track = track
        self.sha256 = sha256


class PublicationConflict(RuntimeError):
    pass


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

    def process(self, lease: JobLease) -> ProcessOutcome:
        staging: Path | None = None
        publication: PublicationResult | None = None
        monitor = LeaseMonitor(self.queue, lease)
        try:
            with monitor:
                self.settings.downloads_path.mkdir(parents=True, exist_ok=True)
                ensure_free_space(
                    self.settings.downloads_path,
                    required_bytes=0,
                    reserve_bytes=self.settings.min_free_bytes,
                )
                staging = create_staging_directory(self.settings.downloads_path, lease.job_id)
                cancellation = _CombinedCancellation(monitor.cancel_event, self.shutdown_signal)
                source_url = self._resolve_source(lease, monitor, cancellation)
                monitor.raise_if_unusable()
                self.queue.set_progress(lease, stage=JobStage.DOWNLOADING, progress=0.08)
                progress_callback = self._progress_callback(lease, monitor)
                result = self.ytdlp.download_audio(
                    source_url,
                    staging,
                    max_duration_seconds=self.settings.max_direct_media_seconds,
                    progress_callback=progress_callback,
                    cancel_signal=cancellation,
                )
                monitor.raise_if_unusable()
                media_probe = self._normalize_media(result.path, cancellation)
                self.queue.set_progress(lease, stage=JobStage.RESOLVING_METADATA, progress=0.62)
                tag_values = self._tag_values(
                    lease.approved_snapshot,
                    result.metadata,
                    source_url,
                    job_id=lease.job_id,
                )
                self._validate_canonical_metadata(tag_values, result.metadata, media_probe)
                tag_values = self._resolve_canonical_metadata(tag_values, media_probe)
                self._check_duplicate(lease.job_id, tag_values, media_probe)
                artwork = self._fetch_artwork(lease, monitor, result.metadata, tag_values)
                self.queue.set_progress(lease, stage=JobStage.TAGGING, progress=0.76)
                write_tags(media_probe.path, tag_values, artwork, verify=True)
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
            self.queue.require_review(
                lease,
                reason=reason,
                options=options,
            )
            cleanup_staging_directory(staging)
            return ProcessOutcome(job_id=lease.job_id, status="needs_review")
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
            retryable = isinstance(
                exc, (YtDlpError, WorkerMetadataError, OSError, ArtworkError)
            ) and not isinstance(
                exc,
                (
                    SourceValidationError,
                    TaggingError,
                    UnsupportedMediaFormat,
                    PublicationConflict,
                    MediaValidationError,
                ),
            )
            status = self.queue.fail(
                lease,
                error_code=_error_code(exc),
                error_message=redact(str(exc) or type(exc).__name__),
                retryable=retryable,
            )
            if status == "failed":
                cleanup_staging_directory(staging)
            logger.warning(
                "download job failed (%s): %s",
                status,
                redact(exc),
                extra={"job_id": lease.job_id},
            )
            return ProcessOutcome(job_id=lease.job_id, status=status)

    def _resolve_source(
        self,
        lease: JobLease,
        monitor: LeaseMonitor,
        cancellation: CancellationSignal,
    ) -> str:
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
        payload = {
            "request_id": request_id,
            "job_id": lease.job_id,
            "intent": {
                "artist": _required_string(lease.approved_snapshot, "artist"),
                "title": _required_string(lease.approved_snapshot, "title"),
                "album": _string_or_none(lease.approved_snapshot.get("album")),
                "version": (
                    _string_or_none(lease.approved_snapshot.get("version_signature")) or "studio"
                ),
                "duration_seconds": _float_or_none(lease.approved_snapshot.get("duration_seconds")),
            },
            "candidates": [
                {
                    "source_id": candidate.source_id,
                    "title": candidate.title,
                    "channel": candidate.channel,
                    "duration_seconds": candidate.duration_seconds,
                }
                for candidate in candidates
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self.session_factory.begin() as session:
            task = session.scalar(
                select(ServiceTask)
                .where(
                    ServiceTask.target == "web",
                    ServiceTask.kind == "select_source",
                    ServiceTask.payload_json == serialized,
                )
                .order_by(ServiceTask.created_at.desc())
                .limit(1)
            )
            if task is None:
                task = ServiceTask(
                    target="web",
                    kind="select_source",
                    payload_json=serialized,
                    available_at=datetime.now(UTC),
                )
                session.add(task)
                session.flush()
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
    ) -> MediaProbe:
        if self.media is None:
            raise MediaValidationError("ffmpeg/ffprobe media verification is not configured")
        return self.media.normalize_and_verify(
            path,
            max_duration_seconds=self.settings.max_direct_media_seconds,
            allow_lossy_transcode=self.settings.allow_lossy_transcode,
            cancel_signal=cancellation,
        )

    def _validate_canonical_metadata(
        self,
        expected: dict[str, Any],
        source: Mapping[str, Any],
        probe: MediaProbe,
    ) -> None:
        expected_artist = _required_string(expected, "artist")
        expected_title = _required_string(expected, "title")
        provider_artist = _first_source_string(source, "artist", "creator", "uploader", "channel")
        provider_title = _first_source_string(source, "track", "alt_title", "title")
        if provider_artist is None or provider_title is None:
            raise JobNeedsReview("the downloaded source did not expose enough metadata to match")
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
            title=provider_title,
            album=_first_source_string(source, "album"),
            duration_seconds=probe.duration_seconds,
            source="youtube",
            raw=source,
        )
        ranked = self.metadata_matcher.rank(
            artist=expected_artist,
            title=expected_title,
            album=_string_or_none(expected.get("album")),
            duration_seconds=_float_or_none(expected.get("duration_seconds")),
            candidates=[candidate],
            limit=1,
        )
        match = ranked[0]
        if match.decision != "auto":
            raise JobNeedsReview(
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
            reviewed_duplicate = session.scalar(
                select(JobReviewOption.id).where(
                    JobReviewOption.job_id == job_id,
                    JobReviewOption.kind == "duplicate",
                    JobReviewOption.selected_at.is_not(None),
                )
            )
        if decision.status == "possible" and reviewed_duplicate is None:
            raise JobNeedsReview(
                decision.reason or "a possible duplicate requires review",
                [
                    {
                        "kind": "duplicate",
                        "rank": 1,
                        "track_id": decision.track_id,
                        "score": 1.0,
                        "reason": decision.reason,
                    }
                ],
            )
        if decision.status == "owned" and track is not None:
            existing = self.settings.music_path / track.filepath
            digest = track.file_sha256 or sha256_file(existing)
            raise DuplicateOwned(track, digest)

    def _resolve_canonical_metadata(
        self,
        values: dict[str, Any],
        probe: MediaProbe,
    ) -> dict[str, Any]:
        if self.metadata_resolver is None:
            raise RuntimeError("MusicBrainz canonical metadata resolution is not configured")
        resolution = self.metadata_resolver.resolve(
            artist=_required_string(values, "artist"),
            title=_required_string(values, "title"),
            album=_string_or_none(values.get("album")),
            duration_seconds=probe.duration_seconds,
            version_signature=_string_or_none(values.get("version_signature")) or "studio",
        )
        if resolution.decision != "auto" or resolution.candidate is None:
            raise JobNeedsReview(
                resolution.reason,
                list(resolution.options) if resolution.decision == "review" else [],
            )
        candidate = resolution.candidate
        enriched = dict(values)
        enriched["artist"] = candidate.artist
        enriched["artists"] = [candidate.artist]
        enriched["title"] = candidate.title
        if candidate.album:
            enriched["album"] = candidate.album
        enriched["album_artist"] = candidate.artist
        enriched["album_artists"] = [candidate.artist]
        if candidate.year is not None:
            enriched["year"] = candidate.year
        for key in ("recording_mbid", "release_mbid", "release_group_mbid"):
            value = getattr(candidate, key)
            if value:
                enriched[key] = value
        return enriched

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
        values["source_extractor"] = _string_or_none(source_metadata.get("extractor")) or "youtube"
        values["source_id"] = _string_or_none(source_metadata.get("id"))
        values["job_id"] = job_id
        return values

    def _progress_callback(self, lease: JobLease, monitor: LeaseMonitor) -> Any:
        lock = threading.Lock()
        last_update = 0.0
        last_progress = 0.08

        def callback(progress: DownloadProgress) -> None:
            nonlocal last_update, last_progress
            monitor.raise_if_unusable()
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


def _string_or_none(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    result: list[str] = []
    for character in name:
        if character.isupper() and result:
            result.append("_")
        result.append(character.casefold())
    return "".join(result)[:80]
