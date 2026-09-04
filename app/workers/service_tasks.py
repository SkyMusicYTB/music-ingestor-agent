from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
    is_curated_collection_url,
    validate_public_media_metadata,
)
from app.db.models import (
    EvidenceReference,
    Request,
    RequestTrack,
    ServiceTask,
    SourceCandidate,
)
from app.repositories.events import make_event
from app.services.artist_credits import structured_artists
from app.services.duplicates import (
    normalize_text,
    normalize_version_signature,
    strip_provider_suffixes,
    version_signature,
)
from app.services.library_scan import LibraryScanner, ScanAlreadyRunning
from app.services.recording_versions import recording_version_evidence
from app.services.request_constraints import ExplicitRequestConstraints
from app.sources import (
    EXECUTABLE_EVIDENCE_KINDS,
    ProviderIdentity,
    UploaderRelationship,
    bound_provider_description,
    classify_version,
    provider_capability,
    provider_for_extractor,
    provider_for_url,
    resolve_provider_recording_metadata,
)
from app.tools.youtube import YouTubeTool
from app.workers.queue import (
    DownloadJobQueue,
    LeaseLostError,
    ServiceTaskLease,
    ServiceTaskQueue,
)
from app.workers.source_failures import is_transient_source_error

logger = logging.getLogger(__name__)
_SAFE_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SOURCE_DISCOVERY_DIAGNOSTICS_KIND = "source_search_diagnostics"
_MAX_SOURCE_DIAGNOSTIC_RUNS = 10
_DIAGNOSTIC_QUERY_SECRET = re.compile(
    r"(?:https?://|\b(?:authorization|bearer|cookie|credential|password|secret|"
    r"api[_ -]?key|token)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ServiceTaskOutcome:
    task_id: str
    kind: str
    completed: bool


class WorkerServiceTaskHandler:
    """Fixed allowlist for non-download work assigned to the worker service."""

    def __init__(
        self,
        *,
        queue: ServiceTaskQueue,
        factory: sessionmaker[Session],
        ytdlp: YtDlpClient,
        youtube: YouTubeTool,
        scanner: LibraryScanner,
        max_duration_seconds: int,
        shutdown_signal: CancellationSignal | None = None,
        download_queue: DownloadJobQueue | None = None,
        source_probe_negative_ttl_seconds: int = 86_400,
        enabled_providers: Iterable[str] = ("bandcamp", "soundcloud", "youtube"),
        max_direct_playlist_items: int = 25,
    ) -> None:
        self.queue = queue
        self.factory = factory
        self.ytdlp = ytdlp
        self.youtube = youtube
        self.scanner = scanner
        self.max_duration_seconds = max_duration_seconds
        self.shutdown_signal = shutdown_signal
        self.download_queue = download_queue
        self.source_probe_negative_ttl_seconds = max(60, source_probe_negative_ttl_seconds)
        self.enabled_providers = frozenset(value.casefold() for value in enabled_providers)
        if not self.enabled_providers:
            raise ValueError("at least one media provider must be enabled")
        if isinstance(max_direct_playlist_items, bool) or not 1 <= max_direct_playlist_items <= 100:
            raise ValueError("direct collection item limit must be between 1 and 100")
        self.max_direct_playlist_items = max_direct_playlist_items

    def process(self, lease: ServiceTaskLease) -> ServiceTaskOutcome:
        monitor = _ServiceLeaseMonitor(self.queue, lease)
        try:
            with monitor:
                result = self._dispatch(lease)
                monitor.raise_if_lost()
            self.queue.complete(lease, result)
            return ServiceTaskOutcome(lease.task_id, lease.kind, True)
        except LeaseLostError:
            return ServiceTaskOutcome(lease.task_id, lease.kind, False)
        except ScanAlreadyRunning:
            self.queue.defer_library_scan(lease)
            return ServiceTaskOutcome(lease.task_id, lease.kind, False)
        except Exception as exc:
            monitor.stop()
            if (
                self.shutdown_signal is not None
                and self.shutdown_signal.is_set()
                and isinstance(exc, (DownloadCancelled, InterruptedError))
            ):
                try:
                    self.queue.release_for_shutdown(lease)
                except LeaseLostError:
                    pass
                return ServiceTaskOutcome(lease.task_id, lease.kind, False)
            retryable = isinstance(exc, OSError) or is_transient_source_error(exc)
            safe_error = _safe_task_error(exc)
            terminal = self.queue.fail(
                lease,
                safe_error,
                retryable=retryable,
            )
            if terminal and lease.kind == "resolve_direct_request":
                self._fail_direct_request(lease.payload, exc, safe_error)
            logger.warning("worker service task %s failed", lease.kind)
            return ServiceTaskOutcome(lease.task_id, lease.kind, False)

    def _dispatch(self, lease: ServiceTaskLease) -> dict[str, Any]:
        if lease.payload_version != 1:
            raise ValueError("unsupported worker task payload version")
        if lease.kind == "resolve_direct_request":
            return self._resolve_direct_request(lease.payload)
        if lease.kind in {"youtube_search", "search_youtube"}:
            return self._youtube_search(lease.payload)
        if lease.kind == "search_media_sources":
            return self._search_media_sources(lease.payload)
        if lease.kind == "probe_media_source":
            return self._probe_media_source(lease.payload)
        if lease.kind == "library_scan":
            full = lease.payload.get("full", False)
            if not isinstance(full, bool):
                raise ValueError("library_scan.full must be a boolean")
            scan = self.scanner.run(
                full=full, cancel_signal=self.shutdown_signal, service_task_id=lease.task_id
            )
            result = {
                "scan_id": scan.id,
                "kind": scan.kind,
                "status": scan.status,
                "scanned_files": scan.scanned_files,
                "changed_files": scan.changed_files,
                "error_count": scan.error_count,
            }
            if self.download_queue is not None:
                result["reconciled_jobs"] = self.download_queue.adopt_published_jobs(
                    self.scanner.music_root
                )
            return result
        raise ValueError(f"unsupported worker service task kind: {lease.kind}")

    def _resolve_direct_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("resolve_direct_request requires request_id")
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise ValueError("direct request was not found")
            raw_url = request.raw_text.strip()
            collection_url = is_curated_collection_url(raw_url)
            if collection_url:
                request.input_kind = "media_collection_url"
                request.requested_count = None
            existing = session.scalar(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
                .limit(1)
            )
            if existing is not None:
                track_id = existing.id
            else:
                track_id = None

        if track_id is not None:
            with self.factory.begin() as session:
                _ensure_confirmation_task(session, request_id)
            return {"request_id": request_id, "request_track_id": track_id, "reused": True}

        if collection_url:
            collection = self.ytdlp.inspect_collection(
                raw_url,
                limit=self.max_direct_playlist_items,
                cancel_signal=self.shutdown_signal,
            )
            return self._store_direct_collection(request_id, raw_url, collection)

        validated = self.ytdlp.validate_url(raw_url)
        metadata = self.ytdlp.probe(validated, cancel_signal=self.shutdown_signal)
        validate_public_media_metadata(metadata)
        duration = _positive_float(metadata.get("duration"))
        if duration is not None and duration > self.max_duration_seconds:
            raise ValueError("direct media exceeds the configured duration limit")
        source_id = _string(metadata.get("id"))
        if source_id is None:
            raise ValueError("provider metadata did not contain a source ID")
        raw_title = _string(metadata.get("title")) or _string(metadata.get("track"))
        recording = resolve_provider_recording_metadata(metadata, fallback_title=raw_title)
        artist = recording.artist
        title = strip_provider_suffixes(recording.title) if recording.title is not None else None
        if title is None or artist is None:
            raise ValueError("provider metadata did not identify the recording artist and title")
        provider = provider_for_url(validated)
        if provider is None:
            raise ValueError("direct media provider could not be identified")
        _media_provider(provider.value, self.enabled_providers)
        uploader = recording.uploader
        uploader_relationship = _uploader_relationship(artist, uploader, metadata)
        extractor = provider_capability(provider).canonical_extractor
        recording_version = _provider_recording_version(
            metadata,
            recording.title,
            raw_title,
        )
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise ValueError("direct request disappeared")
            existing = session.scalar(
                select(RequestTrack)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
                .limit(1)
            )
            if existing is None:
                existing = RequestTrack(
                    request_id=request_id,
                    ordinal=1,
                    artist=artist[:300],
                    title=title[:300],
                    album=(_string(metadata.get("album")) or "")[:300] or None,
                    album_artist=artist[:300],
                    duration_seconds=duration,
                    source_url=None,
                    source_extractor=extractor,
                    source_id=source_id[:100],
                    version_signature=recording_version,
                    rationale="Resolved directly from the reviewed media URL.",
                    evidence_json=json.dumps(["yt-dlp metadata"], separators=(",", ":")),
                    metadata_confidence=0.80,
                    metadata_provenance_json=json.dumps(
                        {
                            "automatic_association": False,
                            "album_constraint_explicit": False,
                            "source": "validated_direct_provider_metadata",
                            "artists": list(recording.artists),
                            "recording_version": {
                                "signature": recording_version,
                                "source": "provider_recording_metadata",
                            },
                            "request_constraints": ExplicitRequestConstraints(
                                provider=provider.value
                            ).as_provenance(),
                        },
                        separators=(",", ":"),
                    ),
                    selected=True,
                )
                session.add(existing)
                session.flush()
                evidence = EvidenceReference(
                    request_id=request_id,
                    request_track_id=existing.id,
                    provider=provider.value,
                    evidence_kind="direct_user_url",
                    canonical_url=validated,
                    provider_item_id=source_id[:200],
                    status="available",
                    sanitized_metadata_json=json.dumps(
                        {
                            "title": raw_title,
                            "artist": artist,
                            "artists": list(recording.artists),
                            "uploader": uploader,
                            "duration_seconds": duration,
                            "version_signature": recording_version,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                session.add(evidence)
                session.flush()
                session.add(
                    SourceCandidate(
                        evidence_id=evidence.id,
                        request_track_id=existing.id,
                        provider=provider.value,
                        extractor=extractor,
                        source_id=source_id[:200],
                        acquisition_url=validated,
                        provider_title=(raw_title or title)[:500],
                        provider_artist=artist[:300],
                        uploader=uploader[:300] if uploader else None,
                        uploader_relationship=uploader_relationship.value,
                        duration_seconds=duration,
                        version_signature=recording_version,
                        group_key=(
                            f"direct:{normalize_text(artist)}:{normalize_text(title)}:"
                            f"{recording_version}"
                        )[:500],
                        local_score=1.0,
                        policy_status="allowed",
                        probe_status="valid",
                        contradictions_json="[]",
                        sanitized_metadata_json=json.dumps(
                            {
                                "direct_user_url": True,
                                "track": title,
                                "artists": list(recording.artists),
                                "artist_source": recording.artist_source,
                                "version": recording_version,
                            },
                            separators=(",", ":"),
                        ),
                    )
                )
            request.discovered_count = 1
            request.status = "preview"
            _ensure_confirmation_task(session, request_id)
            session.add(
                make_event(
                    session,
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.direct_resolved",
                    message="Direct media request resolved for review",
                )
            )
            track_id = existing.id
        return {"request_id": request_id, "request_track_id": track_id, "reused": False}

    def _store_direct_collection(
        self,
        request_id: str,
        raw_url: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        provider = provider_for_url(raw_url)
        if provider is None:
            raise SourceValidationError("collection provider could not be identified")
        _media_provider(provider.value, self.enabled_providers)
        entries = metadata.get("entries")
        if not isinstance(entries, list) or not entries:
            raise SourceValidationError("collection did not contain selectable tracks")
        if len(entries) > self.max_direct_playlist_items:
            raise SourceValidationError("collection exceeds the configured item limit")
        collection_artists = structured_artists(metadata.get("artists"))
        collection_artist = _first_metadata_string(metadata, "artist", "album_artist") or (
            ", ".join(collection_artists) if collection_artists else None
        )
        collection_title = _optional_bounded_string(metadata.get("title"), 300)
        prepared: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            parsed = self._flat_provider_candidate(provider, entry)
            if parsed is None:
                continue
            source_id = _safe_identifier(parsed.get("source_id"), "collection source ID")
            extractor = _safe_identifier(parsed.get("extractor"), "collection extractor")
            identity = (extractor, source_id)
            if identity in seen:
                continue
            seen.add(identity)
            raw_title = _bounded_string(parsed.get("title"), 500, "collection item title")
            artist = _optional_bounded_string(parsed.get("provider_artist"), 300)
            artists = structured_artists(parsed.get("artists"))
            title = strip_provider_suffixes(raw_title)
            parsed_artist, parsed_title = _split_provider_title(title)
            if artist is None:
                artist, title = parsed_artist, parsed_title
            elif parsed_artist is not None and normalize_text(parsed_artist) == normalize_text(
                artist
            ):
                title = parsed_title
            if artist is None:
                artist = collection_artist
                artists = collection_artists
            if artist is None or not title:
                continue
            duration = _positive_float(parsed.get("duration_seconds"))
            if duration is not None and duration > self.max_duration_seconds:
                continue
            url = _bounded_string(parsed.get("url"), 2_048, "collection item URL")
            uploader = _optional_bounded_string(parsed.get("uploader"), 300)
            recording_version = normalize_version_signature(
                _optional_bounded_string(parsed.get("version_signature"), 100)
            )
            prepared.append(
                {
                    "artist": artist[:300],
                    "artists": list(artists),
                    "title": title[:300],
                    "raw_title": raw_title,
                    "album": collection_title,
                    "duration": duration,
                    "source_id": source_id,
                    "extractor": extractor,
                    "url": url,
                    "uploader": uploader,
                    "version_signature": recording_version,
                }
            )
        if not prepared:
            raise SourceValidationError("collection contained no identifiable permitted tracks")

        track_ids: list[str] = []
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None:
                raise ValueError("direct request disappeared")
            # Preserve collection origin independently of how many usable entries
            # survived bounded inspection. A one-entry collection is still a
            # collection and must never become an implicit exact-track approval.
            request.input_kind = "media_collection_url"
            request.requested_count = None
            existing = session.scalar(
                select(RequestTrack.id)
                .where(RequestTrack.request_id == request_id)
                .order_by(RequestTrack.ordinal)
                .limit(1)
            )
            if existing is not None:
                _ensure_confirmation_task(session, request_id)
                return {
                    "request_id": request_id,
                    "request_track_ids": [existing],
                    "reused": True,
                }
            for ordinal, item in enumerate(prepared, start=1):
                artist = str(item["artist"])
                title = str(item["title"])
                stored_duration = _positive_float(item.get("duration"))
                track = RequestTrack(
                    request_id=request_id,
                    ordinal=ordinal,
                    artist=artist,
                    title=title,
                    album=str(item["album"]) if item["album"] else None,
                    album_artist=artist,
                    duration_seconds=stored_duration,
                    source_url=None,
                    source_extractor=str(item["extractor"]),
                    source_id=str(item["source_id"])[:100],
                    version_signature=str(item["version_signature"]),
                    rationale="Selectable item from the bounded direct collection.",
                    evidence_json=json.dumps(["bounded direct collection"], separators=(",", ":")),
                    metadata_confidence=0.80,
                    metadata_provenance_json=json.dumps(
                        {
                            "automatic_association": False,
                            "album_constraint_explicit": False,
                            "source": "validated_direct_collection_metadata",
                            "artists": item["artists"],
                            "recording_version": {
                                "signature": item["version_signature"],
                                "source": "provider_recording_metadata",
                            },
                            "request_constraints": ExplicitRequestConstraints(
                                provider=provider.value
                            ).as_provenance(),
                        },
                        separators=(",", ":"),
                    ),
                    selected=True,
                )
                session.add(track)
                session.flush()
                evidence = EvidenceReference(
                    request_id=request_id,
                    request_track_id=track.id,
                    provider=provider.value,
                    evidence_kind="direct_collection_item",
                    canonical_url=str(item["url"]),
                    provider_item_id=str(item["source_id"]),
                    status="pending",
                    sanitized_metadata_json=json.dumps(
                        {
                            "title": item["raw_title"],
                            "artist": artist,
                            "artists": item["artists"],
                            "uploader": item["uploader"],
                            "duration_seconds": item["duration"],
                            "version_signature": item["version_signature"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
                session.add(evidence)
                session.flush()
                session.add(
                    SourceCandidate(
                        evidence_id=evidence.id,
                        request_track_id=track.id,
                        provider=provider.value,
                        extractor=str(item["extractor"]),
                        source_id=str(item["source_id"]),
                        acquisition_url=str(item["url"]),
                        provider_title=str(item["raw_title"]),
                        provider_artist=artist,
                        uploader=str(item["uploader"]) if item["uploader"] else None,
                        uploader_relationship="unknown",
                        duration_seconds=stored_duration,
                        version_signature=str(item["version_signature"]),
                        group_key=(
                            f"collection:{provider.value}:{item['extractor']}:{item['source_id']}"
                        )[:500],
                        local_score=0.0,
                        policy_status="pending",
                        probe_status="pending",
                        contradictions_json="[]",
                        sanitized_metadata_json=evidence.sanitized_metadata_json,
                    )
                )
                track_ids.append(track.id)
            request.discovered_count = len(track_ids)
            request.selected_count = len(track_ids)
            request.status = "preview"
            _ensure_confirmation_task(session, request_id)
            session.add(
                make_event(
                    session,
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.collection_resolved",
                    message="Collection ready for bounded track selection",
                    details_json=json.dumps({"count": len(track_ids)}, separators=(",", ":")),
                )
            )
        return {"request_id": request_id, "request_track_ids": track_ids, "reused": False}

    def _youtube_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = payload.get("query")
        if not isinstance(query, str):
            raise ValueError("youtube_search requires a query")
        limit = payload.get("limit", 8)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("youtube_search limit must be between 1 and 8")
        response = self.youtube.search(query, limit=limit, cancel_signal=self.shutdown_signal)
        return {
            "query": response.query,
            "candidates": [
                {
                    "source_id": item.source_id,
                    "source_extractor": item.extractor,
                    "url": item.url,
                    "title": item.title,
                    "channel": item.channel,
                    "duration_seconds": item.duration_seconds,
                }
                for item in response.candidates
            ],
        }

    def _search_media_sources(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent_id = _safe_identifier(payload.get("intent_id"), "search_media_sources intent_id")
        provider = _media_provider(payload.get("provider"), self.enabled_providers)
        limit = payload.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("search_media_sources limit must be between 1 and 10")
        request_id, request_track_id, query = self._load_media_intent(intent_id)

        try:
            if provider is ProviderIdentity.BANDCAMP:
                candidates = self._existing_media_evidence(
                    request_id=request_id,
                    request_track_id=request_track_id,
                    provider=provider,
                    limit=limit,
                )
            else:
                candidates = self._search_provider_candidates(provider, query, limit)
                candidates = self._persist_media_evidence(
                    request_id=request_id,
                    request_track_id=request_track_id,
                    provider=provider,
                    candidates=candidates,
                    limit=limit,
                )
        except DownloadCancelled:
            raise
        except (SourceValidationError, YtDlpError) as exc:
            try:
                self._record_request_source_search(
                    request_id=request_id,
                    request_track_id=request_track_id,
                    provider=provider,
                    query=query,
                    found_count=0,
                    rejection_code=(
                        "transient_provider_search"
                        if is_transient_source_error(exc)
                        else "provider_search_rejected"
                    ),
                )
            except Exception:
                logger.warning("request-scoped source-search diagnostics could not be persisted")
            raise
        try:
            self._record_request_source_search(
                request_id=request_id,
                request_track_id=request_track_id,
                provider=provider,
                query=query,
                found_count=len(candidates),
            )
        except Exception:
            # Diagnostics are useful but must never turn a safe source search into
            # a failed acquisition task or leak database/query details into logs.
            logger.warning("request-scoped source-search diagnostics could not be persisted")
        return {
            "intent_id": intent_id,
            "provider": provider.value,
            "candidates": candidates,
        }

    def _probe_media_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        evidence_id = _safe_identifier(payload.get("evidence_id"), "probe_media_source evidence_id")
        with self.factory() as session:
            evidence = session.get(EvidenceReference, evidence_id)
            if (
                evidence is None
                or evidence.status not in {"pending", "available"}
                or not evidence.canonical_url
                or evidence.evidence_kind not in EXECUTABLE_EVIDENCE_KINDS
            ):
                raise ValueError("media evidence is unknown, unavailable, or expired")
            provider = _media_provider(evidence.provider, self.enabled_providers)
            candidate = session.scalar(
                select(SourceCandidate).where(SourceCandidate.evidence_id == evidence.id).limit(1)
            )
            url = evidence.canonical_url

        try:
            metadata = self.ytdlp.probe(url, cancel_signal=self.shutdown_signal)
            result = self._persist_media_probe(evidence_id, provider, candidate, metadata)
        except (SourceValidationError, YtDlpError) as exc:
            if not is_transient_source_error(exc):
                with self.factory.begin() as session:
                    row = session.get(EvidenceReference, evidence_id)
                    if row is not None:
                        row.status = "rejected"
                        row.negative_reason = "media_probe_failed"
                        row.negative_until = datetime.now(UTC) + timedelta(
                            seconds=self.source_probe_negative_ttl_seconds
                        )
            try:
                self._record_request_source_probe(
                    evidence_id,
                    accepted=False,
                    rejection_code=(
                        "transient_candidate_probe"
                        if is_transient_source_error(exc)
                        else "probe_rejected"
                    ),
                )
            except Exception:
                logger.warning("request-scoped source-probe diagnostics could not be persisted")
            raise
        try:
            self._record_request_source_probe(evidence_id, accepted=True)
        except Exception:
            logger.warning("request-scoped source-probe diagnostics could not be persisted")
        return result

    def _load_media_intent(self, intent_id: str) -> tuple[str, str | None, str]:
        with self.factory() as session:
            request = session.get(Request, intent_id)
            track: RequestTrack | None = None
            if request is None:
                track = session.get(RequestTrack, intent_id)
                if track is None:
                    raise ValueError("media intent does not identify a local request")
                request = session.get(Request, track.request_id)
            elif request is not None:
                track = session.scalar(
                    select(RequestTrack)
                    .where(RequestTrack.request_id == request.id)
                    .order_by(RequestTrack.ordinal)
                    .limit(1)
                )
            if request is None:
                raise ValueError("media intent request disappeared")
            if track is not None:
                query = f"{track.artist} {track.title}"
                track_id = track.id
            else:
                query = _query_from_request_text(request.raw_text)
                track_id = None
            return request.id, track_id, query

    def _record_request_source_search(
        self,
        *,
        request_id: str,
        request_track_id: str | None,
        provider: ProviderIdentity,
        query: str,
        found_count: int,
        rejection_code: str | None = None,
    ) -> None:
        """Persist bounded request-scoped search facts without an executable URL."""

        safe_query = _safe_diagnostic_query(query)
        run = {
            "schema_version": 1,
            "query_variant_count": 1,
            "query_attempts": [
                {
                    "provider": provider.value,
                    "query": safe_query,
                    "found_count": min(10, max(0, found_count)),
                }
            ],
            "found_count": min(10, max(0, found_count)),
            "probed_count": 0,
            "accepted_count": 0,
            "rejection_counts": (
                {rejection_code: 1}
                if rejection_code in {"provider_search_rejected", "transient_provider_search"}
                else {}
            ),
            "stopped_early": False,
        }
        with self.factory.begin() as session:
            row = _request_source_diagnostic_row(
                session,
                request_id=request_id,
                request_track_id=request_track_id,
                create=True,
            )
            assert row is not None
            payload = _json_object(row.sanitized_metadata_json)
            runs = _source_diagnostic_runs(payload)
            run_key = (provider.value, safe_query)
            if not any(_source_diagnostic_run_key(existing) == run_key for existing in runs):
                runs.append(run)
                runs = runs[-_MAX_SOURCE_DIAGNOSTIC_RUNS:]
            payload["discovery_diagnostic_runs"] = runs
            payload["discovery_diagnostics"] = _aggregate_source_diagnostics(
                runs,
                previous=payload.get("discovery_diagnostics"),
            )
            row.sanitized_metadata_json = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )

    def _record_request_source_probe(
        self,
        evidence_id: str,
        *,
        accepted: bool,
        rejection_code: str | None = None,
    ) -> None:
        """Update aggregate probe facts for an orchestration-time finite candidate."""

        with self.factory.begin() as session:
            evidence = session.get(EvidenceReference, evidence_id)
            if evidence is None or evidence.evidence_kind not in EXECUTABLE_EVIDENCE_KINDS:
                return
            row = _request_source_diagnostic_row(
                session,
                request_id=evidence.request_id,
                request_track_id=evidence.request_track_id,
                create=False,
            )
            if row is None:
                return
            payload = _json_object(row.sanitized_metadata_json)
            current = payload.get("discovery_diagnostics")
            diagnostics = dict(current) if isinstance(current, dict) else {}
            diagnostics["schema_version"] = 1
            diagnostics["probed_count"] = min(
                12,
                (_diagnostic_count(diagnostics.get("probed_count"), maximum=12) or 0) + 1,
            )
            if accepted:
                diagnostics["accepted_count"] = min(
                    100,
                    (_diagnostic_count(diagnostics.get("accepted_count"), maximum=100) or 0) + 1,
                )
            if rejection_code in {"probe_rejected", "transient_candidate_probe"}:
                raw_rejections = diagnostics.get("rejection_counts")
                rejections = dict(raw_rejections) if isinstance(raw_rejections, dict) else {}
                rejections[rejection_code] = min(
                    1_000,
                    (_diagnostic_count(rejections.get(rejection_code), maximum=1_000) or 0) + 1,
                )
                diagnostics["rejection_counts"] = rejections
            runs = _source_diagnostic_runs(payload)
            if runs:
                run = next(
                    (
                        item
                        for item in reversed(runs)
                        if _source_diagnostic_run_provider(item) == evidence.provider
                    ),
                    runs[-1],
                )
                run["probed_count"] = min(
                    12,
                    (_diagnostic_count(run.get("probed_count"), maximum=12) or 0) + 1,
                )
                if accepted:
                    run["accepted_count"] = min(
                        100,
                        (_diagnostic_count(run.get("accepted_count"), maximum=100) or 0) + 1,
                    )
                if rejection_code in {"probe_rejected", "transient_candidate_probe"}:
                    latest_rejections = run.get("rejection_counts")
                    latest_counts = (
                        dict(latest_rejections) if isinstance(latest_rejections, dict) else {}
                    )
                    latest_counts[rejection_code] = min(
                        1_000,
                        (_diagnostic_count(latest_counts.get(rejection_code), maximum=1_000) or 0)
                        + 1,
                    )
                    run["rejection_counts"] = latest_counts
                payload["discovery_diagnostic_runs"] = runs
            payload["discovery_diagnostics"] = diagnostics
            row.sanitized_metadata_json = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )

    def _search_provider_candidates(
        self,
        provider: ProviderIdentity,
        query: str,
        limit: int,
    ) -> list[dict[str, object]]:
        if provider is ProviderIdentity.YOUTUBE:
            response = self.youtube.search(
                query,
                limit=limit,
                cancel_signal=self.shutdown_signal,
            )
            return [
                {
                    "source_id": item.source_id,
                    "extractor": item.extractor,
                    "url": item.url,
                    "title": item.title,
                    "provider_artist": None,
                    "uploader": item.channel,
                    "duration_seconds": item.duration_seconds,
                    "description": None,
                }
                for item in response.candidates[:limit]
            ]

        provider_payload = self.ytdlp.search_provider(
            query,
            provider=provider,
            limit=limit,
            cancel_signal=self.shutdown_signal,
        )
        entries = provider_payload.get("entries")
        if not isinstance(entries, list):
            return []
        result: list[dict[str, object]] = []
        for entry in entries[:limit]:
            parsed = self._flat_provider_candidate(provider, entry)
            if parsed is not None:
                result.append(parsed)
        return result

    def _flat_provider_candidate(
        self,
        provider: ProviderIdentity,
        value: object,
    ) -> dict[str, object] | None:
        if not isinstance(value, dict):
            return None
        try:
            validate_public_media_metadata(value)
        except SourceValidationError:
            return None
        source_id = _string(value.get("id"))
        title = _string(value.get("title"))
        if source_id is None or title is None or not _is_safe_identifier(source_id):
            return None
        url = _provider_page_url(provider, value, source_id)
        if url is None:
            return None
        try:
            validated_url = self.ytdlp.validate_url(url)
        except SourceValidationError:
            return None
        duration = _positive_float(value.get("duration"))
        if duration is not None and duration > self.max_duration_seconds:
            return None
        extractor = (
            _string(value.get("extractor")) or provider_capability(provider).canonical_extractor
        )
        if provider_for_extractor(extractor.casefold()) not in {None, provider}:
            return None
        recording = resolve_provider_recording_metadata(value, fallback_title=title)
        recording_version = _provider_recording_version(
            value,
            recording.title,
            title,
        )
        return {
            "source_id": source_id[:200],
            "extractor": provider_capability(provider).canonical_extractor,
            "url": validated_url,
            "title": title[:500],
            "provider_artist": recording.artist,
            "artists": list(recording.artists),
            "provider_artist_source": recording.artist_source,
            "provider_track": recording.title,
            "uploader": recording.uploader,
            "duration_seconds": duration,
            "description": bound_provider_description(value.get("description")),
            "version_signature": recording_version,
        }

    def _persist_media_evidence(
        self,
        *,
        request_id: str,
        request_track_id: str | None,
        provider: ProviderIdentity,
        candidates: list[dict[str, object]],
        limit: int,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        with self.factory.begin() as session:
            for value in candidates[:limit]:
                source_id = _safe_identifier(value.get("source_id"), "provider source ID")
                extractor = _safe_identifier(value.get("extractor"), "provider extractor")
                title = _bounded_string(value.get("title"), 500, "provider title")
                url = _bounded_string(value.get("url"), 2048, "provider URL")
                uploader = _optional_bounded_string(value.get("uploader"), 300)
                provider_artist = _optional_bounded_string(value.get("provider_artist"), 300)
                provider_artists = structured_artists(value.get("artists"))
                provider_artist_source = _artist_source(value.get("provider_artist_source"))
                provider_track = _optional_bounded_string(value.get("provider_track"), 300)
                duration = _positive_float(value.get("duration_seconds"))
                description = _optional_bounded_string(value.get("description"), 2_000)
                version = normalize_version_signature(
                    _optional_bounded_string(value.get("version_signature"), 100)
                    or classify_version(title, provider_track).signature
                )
                row = session.scalar(
                    select(SourceCandidate)
                    .join(EvidenceReference, SourceCandidate.evidence_id == EvidenceReference.id)
                    .where(
                        EvidenceReference.request_id == request_id,
                        _evidence_track_scope(request_track_id),
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                        SourceCandidate.provider == provider.value,
                        SourceCandidate.extractor == extractor,
                        SourceCandidate.source_id == source_id,
                    )
                    .order_by(SourceCandidate.created_at)
                    .limit(1)
                )
                sanitized = {
                    "title": title,
                    "provider_artist": provider_artist,
                    "artists": list(provider_artists),
                    "artist_source": provider_artist_source,
                    "track": provider_track,
                    "uploader": uploader,
                    "duration_seconds": duration,
                    "description": description,
                    "version_signature": version,
                }
                evidence_row: EvidenceReference | None
                if row is None:
                    evidence_row = EvidenceReference(
                        request_id=request_id,
                        request_track_id=request_track_id,
                        provider=provider.value,
                        evidence_kind="provider_search_result",
                        canonical_url=url,
                        provider_item_id=source_id,
                        status="pending",
                        sanitized_metadata_json=json.dumps(
                            sanitized,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                    session.add(evidence_row)
                    session.flush()
                    # Descriptions are retained for display/audit, but they are
                    # untrusted provider text and cannot change match semantics.
                    row = SourceCandidate(
                        evidence_id=evidence_row.id,
                        request_track_id=request_track_id,
                        provider=provider.value,
                        extractor=extractor,
                        source_id=source_id,
                        acquisition_url=url,
                        provider_title=title,
                        provider_artist=provider_artist,
                        uploader=uploader,
                        uploader_relationship="unknown",
                        duration_seconds=duration,
                        version_signature=version,
                        group_key=f"{provider.value}:{extractor}:{source_id}"[:500],
                        local_score=0.0,
                        policy_status="pending",
                        probe_status="pending",
                        sanitized_metadata_json=json.dumps(
                            sanitized,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                    session.add(row)
                    session.flush()
                else:
                    evidence_row = session.get(EvidenceReference, row.evidence_id)
                    if evidence_row is None:
                        continue
                    if request_track_id is not None and row.request_track_id is None:
                        row.request_track_id = request_track_id
                        evidence_row.request_track_id = request_track_id
                results.append(
                    {
                        "evidence_id": evidence_row.id,
                        "provider": provider.value,
                        "title": title,
                        "uploader": uploader,
                        "duration_seconds": duration,
                    }
                )
        return results

    def _existing_media_evidence(
        self,
        *,
        request_id: str,
        request_track_id: str | None,
        provider: ProviderIdentity,
        limit: int,
    ) -> list[dict[str, object]]:
        with self.factory() as session:
            rows = list(
                session.scalars(
                    select(EvidenceReference)
                    .where(
                        EvidenceReference.request_id == request_id,
                        _evidence_track_scope(request_track_id),
                        EvidenceReference.evidence_kind.in_(EXECUTABLE_EVIDENCE_KINDS),
                        EvidenceReference.provider == provider.value,
                        EvidenceReference.status.in_(["pending", "available"]),
                        EvidenceReference.canonical_url.is_not(None),
                    )
                    .order_by(EvidenceReference.created_at)
                    .limit(limit)
                )
            )
        results: list[dict[str, object]] = []
        for row in rows:
            metadata = _json_object(row.sanitized_metadata_json)
            title = _optional_bounded_string(metadata.get("title"), 500)
            if title is None:
                continue
            results.append(
                {
                    "evidence_id": row.id,
                    "provider": provider.value,
                    "title": title,
                    "uploader": _optional_bounded_string(metadata.get("uploader"), 300),
                    "duration_seconds": _positive_float(metadata.get("duration_seconds")),
                }
            )
        return results

    def _persist_media_probe(
        self,
        evidence_id: str,
        provider: ProviderIdentity,
        candidate: SourceCandidate | None,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        validate_public_media_metadata(metadata)
        source_id = _safe_identifier(metadata.get("id"), "probed provider source ID")
        title = _bounded_string(
            metadata.get("track") or metadata.get("title"), 500, "probed provider title"
        )
        recording = resolve_provider_recording_metadata(metadata, fallback_title=title)
        provider_artist = recording.artist
        uploader = recording.uploader
        duration = _positive_float(metadata.get("duration"))
        if duration is not None and duration > self.max_duration_seconds:
            raise SourceValidationError("media evidence exceeds the configured duration limit")
        extractor_value = _string(metadata.get("extractor")) or _string(
            metadata.get("extractor_key")
        )
        if (
            extractor_value is None
            or provider_for_extractor(extractor_value.casefold()) is not provider
        ):
            raise SourceValidationError("media probe returned a mismatched extractor")
        description = bound_provider_description(metadata.get("description"))
        version = _provider_recording_version(metadata, recording.title, title)

        with self.factory.begin() as session:
            evidence = session.get(EvidenceReference, evidence_id)
            if (
                evidence is None
                or not evidence.canonical_url
                or evidence.evidence_kind not in EXECUTABLE_EVIDENCE_KINDS
            ):
                raise ValueError("media evidence disappeared")
            row = session.get(SourceCandidate, candidate.id) if candidate is not None else None
            if row is None:
                row = SourceCandidate(
                    evidence_id=evidence.id,
                    request_track_id=evidence.request_track_id,
                    provider=provider.value,
                    extractor=provider_capability(provider).canonical_extractor,
                    source_id=source_id,
                    acquisition_url=evidence.canonical_url,
                    provider_title=title,
                    group_key=(
                        f"{provider.value}:{provider_capability(provider).canonical_extractor}:"
                        f"{source_id}"
                    )[:500],
                )
                session.add(row)
            canonical_artist = provider_artist
            if evidence.request_track_id:
                track = session.get(RequestTrack, evidence.request_track_id)
                if track is not None:
                    canonical_artist = track.artist
            relationship = _uploader_relationship(canonical_artist or "", uploader, metadata)
            sanitized = {
                "title": title,
                "provider_artist": provider_artist,
                "artists": list(recording.artists),
                "artist_source": recording.artist_source,
                "track": recording.title,
                "uploader": uploader,
                "duration_seconds": duration,
                "description": description,
                "version_signature": version,
            }
            row.provider = provider.value
            row.extractor = provider_capability(provider).canonical_extractor
            row.source_id = source_id
            row.acquisition_url = evidence.canonical_url
            row.provider_title = title
            row.provider_artist = provider_artist
            row.uploader = uploader
            row.uploader_relationship = relationship.value
            row.duration_seconds = duration
            row.version_signature = version
            row.group_key = f"{provider.value}:{row.extractor}:{source_id}"[:500]
            row.policy_status = "allowed"
            row.probe_status = "valid"
            row.sanitized_metadata_json = json.dumps(
                sanitized,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            evidence.provider_item_id = source_id
            evidence.status = "available"
            evidence.negative_reason = None
            evidence.negative_until = None
            evidence.sanitized_metadata_json = row.sanitized_metadata_json
            session.flush()
            return {
                "evidence_id": evidence.id,
                "source_candidate_id": row.id,
                "provider": provider.value,
                "title": title,
                "provider_artist": provider_artist,
                "uploader": uploader,
                "duration_seconds": duration,
                "uploader_relationship": relationship.value,
                "version_signature": version,
            }

    def _fail_direct_request(
        self,
        payload: dict[str, Any],
        error: Exception,
        safe_message: str,
    ) -> None:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        error_code = (
            "invalid_source_url"
            if isinstance(error, SourceValidationError)
            else "source_resolution_failed"
        )
        with self.factory.begin() as session:
            request = session.get(Request, request_id)
            if request is None or request.status in {"auto_queued", "queued"}:
                return
            request.status = "failed"
            request.error_code = error_code
            request.error_message = safe_message[:500]
            session.add(
                make_event(
                    session,
                    entity_type="request",
                    entity_id=request_id,
                    event_type="request.direct_failed",
                    message="Direct media request could not be resolved",
                )
            )


class _ServiceLeaseMonitor:
    def __init__(self, queue: ServiceTaskQueue, lease: ServiceTaskLease) -> None:
        self.queue = queue
        self.lease = lease
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.error: LeaseLostError | None = None

    def __enter__(self) -> _ServiceLeaseMonitor:
        self.thread = threading.Thread(target=self._run, name="service-task-lease", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def raise_if_lost(self) -> None:
        if self.error is not None:
            raise self.error

    def _run(self) -> None:
        interval = max(2.0, min(20.0, self.queue.lease_seconds / 3))
        while not self.stop_event.wait(interval):
            try:
                self.queue.heartbeat(self.lease)
            except LeaseLostError as exc:
                self.error = exc
                return


def _request_source_diagnostic_row(
    session: Session,
    *,
    request_id: str | None,
    request_track_id: str | None,
    create: bool,
) -> EvidenceReference | None:
    if not isinstance(request_id, str) or not request_id:
        return None
    track_scope = (
        EvidenceReference.request_track_id == request_track_id
        if request_track_id is not None
        else EvidenceReference.request_track_id.is_(None)
    )
    row = session.scalar(
        select(EvidenceReference)
        .where(
            EvidenceReference.request_id == request_id,
            EvidenceReference.job_id.is_(None),
            EvidenceReference.evidence_kind == _SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
            track_scope,
        )
        .order_by(EvidenceReference.created_at, EvidenceReference.id)
        .limit(1)
    )
    if row is not None or not create:
        return row
    row = EvidenceReference(
        request_id=request_id,
        request_track_id=request_track_id,
        job_id=None,
        provider="automatic",
        evidence_kind=_SOURCE_DISCOVERY_DIAGNOSTICS_KIND,
        canonical_url=None,
        provider_item_id=None,
        status="available",
        sanitized_metadata_json="{}",
    )
    session.add(row)
    session.flush()
    return row


def _safe_diagnostic_query(value: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(normalized.split())[:300]
    if not normalized or _DIAGNOSTIC_QUERY_SECRET.search(normalized):
        return "[redacted unsafe query]"
    return normalized


def _diagnostic_count(value: object, *, maximum: int) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum:
        return value
    return None


def _source_diagnostic_runs(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_runs = payload.get("discovery_diagnostic_runs")
    if not isinstance(raw_runs, list):
        return []
    return [dict(run) for run in raw_runs[-_MAX_SOURCE_DIAGNOSTIC_RUNS:] if isinstance(run, dict)]


def _source_diagnostic_run_key(value: dict[str, object]) -> tuple[str, str] | None:
    attempts = value.get("query_attempts")
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[0], dict):
        return None
    provider = attempts[0].get("provider")
    query = attempts[0].get("query")
    if not isinstance(provider, str) or not isinstance(query, str):
        return None
    return provider, query


def _source_diagnostic_run_provider(value: dict[str, object]) -> str | None:
    key = _source_diagnostic_run_key(value)
    return key[0] if key is not None else None


def _aggregate_source_diagnostics(
    runs: list[dict[str, object]],
    *,
    previous: object,
) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for run in runs:
        raw_attempts = run.get("query_attempts")
        if not isinstance(raw_attempts, list):
            continue
        for raw in raw_attempts[:12]:
            if not isinstance(raw, dict):
                continue
            provider = raw.get("provider")
            query = raw.get("query")
            found = _diagnostic_count(raw.get("found_count"), maximum=10)
            if (
                not isinstance(provider, str)
                or provider not in {"bandcamp", "soundcloud", "youtube"}
                or not isinstance(query, str)
                or found is None
            ):
                continue
            attempt_key = provider, _safe_diagnostic_query(query)
            if attempt_key in seen:
                continue
            seen.add(attempt_key)
            attempts.append(
                {"provider": attempt_key[0], "query": attempt_key[1], "found_count": found}
            )
            if len(attempts) >= 12:
                break
        if len(attempts) >= 12:
            break
    prior = previous if isinstance(previous, dict) else {}
    raw_rejections = prior.get("rejection_counts")
    rejections: dict[str, int] = {}
    for rejection_code in (
        "provider_search_rejected",
        "transient_provider_search",
        "probe_rejected",
        "transient_candidate_probe",
    ):
        run_total = 0
        for run in runs:
            counts = run.get("rejection_counts")
            if isinstance(counts, dict):
                run_total += _diagnostic_count(counts.get(rejection_code), maximum=1_000) or 0
        prior_count = (
            _diagnostic_count(
                raw_rejections.get(rejection_code) if isinstance(raw_rejections, dict) else None,
                maximum=1_000,
            )
            or 0
        )
        if rejection_count := min(1_000, max(run_total, prior_count)):
            rejections[rejection_code] = rejection_count
    return {
        "schema_version": 1,
        "query_variant_count": min(24, len({attempt["query"] for attempt in attempts})),
        "query_attempts": attempts,
        "found_count": min(
            1_000,
            sum(
                found_count
                for attempt in attempts
                if (found_count := _diagnostic_count(attempt.get("found_count"), maximum=10))
                is not None
            ),
        ),
        "probed_count": _diagnostic_count(prior.get("probed_count"), maximum=12) or 0,
        "accepted_count": _diagnostic_count(prior.get("accepted_count"), maximum=100) or 0,
        "rejection_counts": rejections,
        "stopped_early": False,
    }


def _media_provider(
    value: object,
    enabled_providers: frozenset[str] | None = None,
) -> ProviderIdentity:
    if not isinstance(value, str):
        raise ValueError("media provider must be a string")
    try:
        provider = ProviderIdentity(value)
    except ValueError as exc:
        raise ValueError("media provider is unsupported") from exc
    if provider not in {
        ProviderIdentity.YOUTUBE,
        ProviderIdentity.SOUNDCLOUD,
        ProviderIdentity.BANDCAMP,
    }:
        raise ValueError("media provider is not enabled for acquisition")
    if enabled_providers is not None and provider.value not in enabled_providers:
        raise ValueError("media provider is disabled")
    return provider


def _is_safe_identifier(value: str) -> bool:
    return bool(_SAFE_MEDIA_ID.fullmatch(value))


def _safe_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _is_safe_identifier(value):
        raise ValueError(f"{label} must be a bounded opaque identifier")
    return value


def _bounded_string(value: object, limit: int, label: str) -> str:
    result = _optional_bounded_string(value, limit)
    if result is None:
        raise ValueError(f"{label} must be a non-empty bounded string")
    return result


def _optional_bounded_string(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


def _first_metadata_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        result = _optional_bounded_string(value.get(key), 300)
        if result is not None:
            return result
    return None


def _provider_page_url(
    provider: ProviderIdentity,
    value: dict[str, Any],
    source_id: str,
) -> str | None:
    if provider is ProviderIdentity.YOUTUBE:
        return f"https://www.youtube.com/watch?v={source_id}"
    for key in ("webpage_url", "original_url", "url"):
        candidate = _optional_bounded_string(value.get(key), 2_048)
        if candidate is not None and provider_for_url(candidate) is provider:
            return candidate
    return None


def _query_from_request_text(value: str) -> str:
    normalized = " ".join(value.split())
    normalized = re.sub(
        r"^(?:please\s+)?(?:add|find|download|get)\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    by_match = re.fullmatch(r"(.+?)\s+by\s+(.+)", normalized, flags=re.IGNORECASE)
    if by_match is not None:
        normalized = f"{by_match.group(2)} {by_match.group(1)}"
    if not normalized:
        raise ValueError("media request did not contain a searchable intent")
    return normalized[:300]


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _provider_recording_version(
    metadata: dict[str, Any],
    *resolved_titles: str | None,
) -> str:
    """Classify only provider recording fields, never album or uploader text."""

    recording_values = (
        *resolved_titles,
        _string(metadata.get("track")),
        _string(metadata.get("alt_title")),
        _string(metadata.get("title")),
    )
    return normalize_version_signature(
        version_signature(
            _string(metadata.get("version")),
            *recording_version_evidence(*recording_values),
        )
    )


def _artist_source(value: object) -> str | None:
    if isinstance(value, str) and value in {
        "artist",
        "album_artist",
        "creator",
        "parsed_title",
    }:
        return value
    return None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result > 0 else None


def _split_provider_title(value: str) -> tuple[str | None, str]:
    for separator in (" - ", " \u2013 ", " \u2014 "):
        if separator in value:
            artist, title = value.split(separator, 1)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip()
    return None, value


def _uploader_relationship(
    canonical_artist: str,
    uploader: str | None,
    metadata: dict[str, Any],
) -> UploaderRelationship:
    if uploader is None:
        return UploaderRelationship.UNKNOWN
    normalized_uploader = normalize_text(uploader)
    normalized_artist = normalize_text(canonical_artist)
    if normalized_uploader.endswith(" topic"):
        return UploaderRelationship.TOPIC
    if normalized_artist and (
        normalized_uploader == normalized_artist
        or normalized_uploader.startswith(f"{normalized_artist} official")
    ):
        return UploaderRelationship.OFFICIAL_ARTIST
    description = normalize_text(_string(metadata.get("description")) or "")
    if "provided to" in description or "distributed by" in description:
        return UploaderRelationship.DISTRIBUTOR
    return UploaderRelationship.THIRD_PARTY


def _evidence_track_scope(request_track_id: str | None) -> Any:
    if request_track_id is None:
        return EvidenceReference.request_track_id.is_(None)
    return or_(
        EvidenceReference.request_track_id == request_track_id,
        EvidenceReference.request_track_id.is_(None),
    )


def _safe_task_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, SourceValidationError)):
        return str(exc)[:1000]
    return f"{type(exc).__name__}: worker task failed"


def _ensure_confirmation_task(session: Session, request_id: str) -> None:
    active = session.scalar(
        select(ServiceTask.id)
        .where(
            ServiceTask.target == "web",
            ServiceTask.kind == "confirm_request",
            ServiceTask.state.in_(["queued", "running", "retry_wait"]),
            ServiceTask.payload_json
            == json.dumps({"request_id": request_id}, separators=(",", ":")),
        )
        .limit(1)
    )
    if active is not None:
        return
    session.add(
        ServiceTask(
            target="web",
            kind="confirm_request",
            payload_json=json.dumps({"request_id": request_id}, separators=(",", ":")),
            available_at=datetime.now(UTC),
        )
    )
