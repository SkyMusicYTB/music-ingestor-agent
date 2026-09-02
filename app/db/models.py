from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.ids import uuid7


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    username: Mapped[str] = mapped_column(String(80))
    username_normalized: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_expires", "absolute_expires_at", "idle_expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship()


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Request(TimestampMixin, Base):
    __tablename__ = "requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_requests_user_idempotency"),
        CheckConstraint("action IN ('find','add')", name="ck_requests_action"),
        CheckConstraint(
            "status IN ('pending','orchestrating','preview','auto_queued','queued','degraded',"
            "'needs_clarification','refused','incomplete','failed')",
            name="ck_requests_status",
        ),
        Index("ix_requests_conversation_created", "conversation_id", "created_at"),
        Index("ix_requests_orchestration", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    refinement_parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("requests.id", ondelete="SET NULL")
    )
    raw_text: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(16))
    input_kind: Mapped[str] = mapped_column(String(32), default="natural_language")
    requested_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    prompt_version: Mapped[str] = mapped_column(String(64), default="orchestrator_v1")
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(500))


class RequestTrack(TimestampMixin, Base):
    __tablename__ = "request_tracks"
    __table_args__ = (
        UniqueConstraint("request_id", "ordinal", name="uq_request_tracks_ordinal"),
        CheckConstraint("duplicate_status IN ('none','owned','possible')", name="ck_track_dup"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    artist: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    album: Mapped[str | None] = mapped_column(String(300))
    album_artist: Mapped[str | None] = mapped_column(String(300))
    year: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    recording_mbid: Mapped[str | None] = mapped_column(String(36), index=True)
    release_mbid: Mapped[str | None] = mapped_column(String(36), index=True)
    release_group_mbid: Mapped[str | None] = mapped_column(String(36), index=True)
    suggested_recording_mbid: Mapped[str | None] = mapped_column(String(36))
    suggested_release_mbid: Mapped[str | None] = mapped_column(String(36))
    suggested_release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    canonical_identity_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_extractor: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(100))
    version_signature: Mapped[str] = mapped_column(String(300), default="studio")
    rationale: Mapped[str] = mapped_column(String(1000), default="")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    duplicate_status: Mapped[str] = mapped_column(String(16), default="none")
    duplicate_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL")
    )
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_confidence: Mapped[float | None] = mapped_column(Float)
    metadata_provenance_json: Mapped[str] = mapped_column(Text, default="{}")


class ServiceTask(TimestampMixin, Base):
    __tablename__ = "service_tasks"
    __table_args__ = (
        CheckConstraint("target IN ('web','worker')", name="ck_service_tasks_target"),
        CheckConstraint(
            "state IN ('queued','running','completed','retry_wait','failed')",
            name="ck_service_tasks_state",
        ),
        Index("ix_service_tasks_claim", "target", "state", "available_at", "created_at"),
        Index("ix_service_tasks_lease", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    target: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(64))
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="queued")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1000))


class DownloadJob(TimestampMixin, Base):
    __tablename__ = "download_jobs"
    __table_args__ = (
        UniqueConstraint("request_track_id", name="uq_download_jobs_request_track"),
        CheckConstraint(
            "status IN ('queued','active','retry_wait','needs_review','waiting_for_space',"
            "'cancel_requested','cancelled','failed','completed')",
            name="ck_download_jobs_status",
        ),
        Index("ix_download_jobs_claim", "status", "available_at", "priority", "created_at"),
        Index("ix_download_jobs_lease", "lease_expires_at"),
        Index(
            "uq_download_jobs_active_dedup",
            "dedup_key",
            unique=True,
            sqlite_where=text("status NOT IN ('cancelled','failed','completed')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    request_track_id: Mapped[str] = mapped_column(
        ForeignKey("request_tracks.id", ondelete="RESTRICT")
    )
    approved_snapshot_json: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    source_extractor: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(100))
    active_source_candidate_id: Mapped[str | None] = mapped_column(String(36))
    source_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    decision_revision: Mapped[int] = mapped_column(Integer, default=0)
    review_round_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    final_track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.id", ondelete="SET NULL"))
    final_relative_path: Mapped[str | None] = mapped_column(String(1200))
    final_sha256: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobReviewOption(Base):
    __tablename__ = "job_review_options"
    __table_args__ = (UniqueConstraint("decision_id", "rank", name="uq_job_review_decision_rank"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    decision_id: Mapped[str | None] = mapped_column(
        ForeignKey("job_decisions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    rank: Mapped[int] = mapped_column(Integer)
    option_key: Mapped[str | None] = mapped_column(String(160))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    materially_different: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_payload_json: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvidenceReference(TimestampMixin, Base):
    __tablename__ = "evidence_references"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','available','unsupported','rejected','expired')",
            name="ck_evidence_references_status",
        ),
        Index("ix_evidence_request_track", "request_track_id", "created_at"),
        Index("ix_evidence_negative_until", "negative_until"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    request_id: Mapped[str | None] = mapped_column(
        ForeignKey("requests.id", ondelete="CASCADE"), index=True
    )
    request_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("request_tracks.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40))
    evidence_kind: Mapped[str] = mapped_column(String(40))
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    provider_item_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    sanitized_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    negative_reason: Mapped[str | None] = mapped_column(String(120))
    negative_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceCandidate(TimestampMixin, Base):
    __tablename__ = "source_candidates"
    __table_args__ = (
        CheckConstraint(
            "policy_status IN ('pending','allowed','rejected','exhausted')",
            name="ck_source_candidates_policy_status",
        ),
        CheckConstraint(
            "probe_status IN ('pending','valid','invalid','failed')",
            name="ck_source_candidates_probe_status",
        ),
        CheckConstraint(
            "uploader_relationship IN ('official_artist','official_label','topic',"
            "'distributor','third_party','unknown')",
            name="ck_source_candidates_uploader_relationship",
        ),
        UniqueConstraint(
            "request_track_id",
            "provider",
            "extractor",
            "source_id",
            name="uq_source_candidates_identity",
        ),
        Index("ix_source_candidates_job_rank", "job_id", "policy_status", "local_score"),
        Index("ix_source_candidates_evidence", "evidence_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    evidence_id: Mapped[str | None] = mapped_column(
        ForeignKey("evidence_references.id", ondelete="SET NULL"), index=True
    )
    request_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("request_tracks.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40))
    extractor: Mapped[str] = mapped_column(String(80))
    source_id: Mapped[str] = mapped_column(String(200))
    acquisition_url: Mapped[str | None] = mapped_column(String(2048))
    provider_title: Mapped[str] = mapped_column(String(500))
    provider_artist: Mapped[str | None] = mapped_column(String(300))
    uploader: Mapped[str | None] = mapped_column(String(300))
    uploader_relationship: Mapped[str] = mapped_column(String(24), default="unknown")
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    version_signature: Mapped[str] = mapped_column(String(300), default="studio")
    group_key: Mapped[str] = mapped_column(String(500))
    local_score: Mapped[float] = mapped_column(Float, default=0.0)
    policy_status: Mapped[str] = mapped_column(String(20), default="pending")
    probe_status: Mapped[str] = mapped_column(String(20), default="pending")
    contradictions_json: Mapped[str] = mapped_column(Text, default="[]")
    sanitized_metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    superseded_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_candidates.id", ondelete="SET NULL")
    )


class JobDecision(Base):
    __tablename__ = "job_decisions"
    __table_args__ = (
        CheckConstraint(
            "category IN ('recording_version','acquisition_source','canonical_metadata',"
            "'possible_duplicate')",
            name="ck_job_decisions_category",
        ),
        CheckConstraint(
            "state IN ('pending','selected','superseded','rejected')",
            name="ck_job_decisions_state",
        ),
        CheckConstraint(
            "decided_by IS NULL OR decided_by IN ('deterministic','openai','user','migration')",
            name="ck_job_decisions_decided_by",
        ),
        UniqueConstraint(
            "job_id",
            "category",
            "candidate_set_fingerprint",
            "revision",
            name="uq_job_decisions_fingerprint_revision",
        ),
        Index("ix_job_decisions_pending", "job_id", "state", "category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(32))
    candidate_set_fingerprint: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    selected_payload_json: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(20))
    openai_call_id: Mapped[str | None] = mapped_column(
        ForeignKey("openai_calls.id", ondelete="SET NULL")
    )
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    local_confidence: Mapped[float | None] = mapped_column(Float)
    model_confidence: Mapped[float | None] = mapped_column(Float)
    contradictions_json: Mapped[str] = mapped_column(Text, default="[]")
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    round_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobArtifact(Base):
    __tablename__ = "job_artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('creating','ready','published','removed','invalid')",
            name="ck_job_artifacts_status",
        ),
        UniqueConstraint("job_id", "kind", "relative_path", name="uq_job_artifacts_path"),
        Index("ix_job_artifacts_recovery", "job_id", "status", "stage"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("download_jobs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40))
    stage: Mapped[str] = mapped_column(String(40))
    relative_path: Mapped[str] = mapped_column(String(1200))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    generation_token: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="creating")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_entity", "entity_type", "entity_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(String(500))
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Album(TimestampMixin, Base):
    __tablename__ = "albums"
    __table_args__ = (
        UniqueConstraint("identity_key", name="uq_albums_identity"),
        Index("ix_albums_mbids", "release_mbid", "release_group_mbid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    identity_key: Mapped[str] = mapped_column(String(900))
    artist: Mapped[str] = mapped_column(String(300))
    artist_normalized: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(300))
    title_normalized: Mapped[str] = mapped_column(String(300))
    year: Mapped[int | None] = mapped_column(Integer)
    release_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    artwork_cache_key: Mapped[str | None] = mapped_column(String(200))


class Track(TimestampMixin, Base):
    __tablename__ = "tracks"
    __table_args__ = (
        Index(
            "ix_tracks_identity",
            "artist_normalized",
            "title_normalized",
            "version_signature",
            "is_present",
        ),
        Index("ix_tracks_mbids", "recording_mbid", "release_mbid"),
        Index(
            "uq_tracks_source",
            "source_extractor",
            "source_id",
            unique=True,
            sqlite_where=text("source_extractor IS NOT NULL AND source_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    album_id: Mapped[str | None] = mapped_column(ForeignKey("albums.id", ondelete="SET NULL"))
    artist: Mapped[str] = mapped_column(String(300))
    artist_normalized: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(300))
    title_normalized: Mapped[str] = mapped_column(String(300))
    album: Mapped[str | None] = mapped_column(String(300))
    album_artist: Mapped[str | None] = mapped_column(String(300))
    genre: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer)
    track_number: Mapped[int | None] = mapped_column(Integer)
    track_total: Mapped[int | None] = mapped_column(Integer)
    disc_number: Mapped[int | None] = mapped_column(Integer)
    disc_total: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    recording_mbid: Mapped[str | None] = mapped_column(String(36))
    release_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    version_signature: Mapped[str] = mapped_column(String(300), default="studio")
    filepath: Mapped[str] = mapped_column(String(1200), unique=True)
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    codec: Mapped[str | None] = mapped_column(String(64))
    bitrate: Mapped[int | None] = mapped_column(Integer)
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger)
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    source_extractor: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(100))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    provenance_json: Mapped[str] = mapped_column(Text, default="{}")
    scan_generation: Mapped[int] = mapped_column(Integer, default=0)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed')", name="ck_scan_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(20))
    generation: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    scanned_files: Mapped[int] = mapped_column(Integer, default=0)
    changed_files: Mapped[int] = mapped_column(Integer, default=0)
    missing_files: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class ArtworkCache(TimestampMixin, Base):
    __tablename__ = "artwork_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_artwork_cache_key"),
        Index("ix_artwork_mbids", "release_mbid", "release_group_mbid"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    cache_key: Mapped[str] = mapped_column(String(200))
    release_mbid: Mapped[str | None] = mapped_column(String(36))
    release_group_mbid: Mapped[str | None] = mapped_column(String(36))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    mime_type: Mapped[str | None] = mapped_column(String(80))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    relative_path: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="ok")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class ExternalCache(Base):
    __tablename__ = "external_cache"
    __table_args__ = (
        UniqueConstraint("namespace", "cache_key", name="uq_external_cache"),
        Index("ix_external_cache_expiry", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    namespace: Mapped[str] = mapped_column(String(80))
    cache_key: Mapped[str] = mapped_column(String(500))
    payload_json: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OpenAICall(Base):
    __tablename__ = "openai_calls"
    __table_args__ = (Index("ix_openai_calls_created_model", "created_at", "model"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("requests.id", ondelete="SET NULL"))
    response_id: Mapped[str | None] = mapped_column(String(100))
    provider_request_id: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(64))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    web_search_count: Mapped[int] = mapped_column(Integer, default=0)
    web_search_context: Mapped[str | None] = mapped_column(String(20))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(100))
    exception_class: Mapped[str | None] = mapped_column(String(120))
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_error_code: Mapped[str | None] = mapped_column(String(120))
    provider_error_parameter: Mapped[str | None] = mapped_column(String(120))
    application_call_id: Mapped[str | None] = mapped_column(String(64))
    failure_phase: Mapped[str | None] = mapped_column(String(40))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    service_tier: Mapped[str | None] = mapped_column(String(40))
    pricing_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    estimated_cost_microusd: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OpenAIToolCall(Base):
    __tablename__ = "openai_tool_calls"
    __table_args__ = (
        UniqueConstraint("openai_call_id", "provider_call_id", name="uq_openai_tool_call"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    openai_call_id: Mapped[str] = mapped_column(
        ForeignKey("openai_calls.id", ondelete="CASCADE"), index=True
    )
    provider_call_id: Mapped[str] = mapped_column(String(100))
    tool_name: Mapped[str] = mapped_column(String(100))
    tool_kind: Mapped[str] = mapped_column(String(20))
    arguments_json: Mapped[str] = mapped_column(Text)
    result_summary_json: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(32), primary_key=True)
    service_version: Mapped[str] = mapped_column(String(64))
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    active_work_count: Mapped[int] = mapped_column(Integer, default=0)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
