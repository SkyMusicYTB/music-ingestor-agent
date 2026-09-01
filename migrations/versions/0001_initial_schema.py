"""Frozen initial production schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "albums",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_key", sa.String(length=900), nullable=False),
        sa.Column("artist", sa.String(length=300), nullable=False),
        sa.Column("artist_normalized", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("title_normalized", sa.String(length=300), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("release_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_group_mbid", sa.String(length=36), nullable=True),
        sa.Column("artwork_cache_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_key", name="uq_albums_identity"),
    )
    with op.batch_alter_table("albums", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_albums_artist_normalized"), ["artist_normalized"], unique=False
        )
        batch_op.create_index(
            "ix_albums_mbids", ["release_mbid", "release_group_mbid"], unique=False
        )

    op.create_table(
        "artwork_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cache_key", sa.String(length=200), nullable=False),
        sa.Column("release_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_group_mbid", sa.String(length=36), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=80), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("relative_path", sa.String(length=500), nullable=True),
        sa.Column("etag", sa.String(length=300), nullable=True),
        sa.Column("last_modified", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key", name="uq_artwork_cache_key"),
    )
    with op.batch_alter_table("artwork_cache", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_artwork_cache_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(
            "ix_artwork_mbids", ["release_mbid", "release_group_mbid"], unique=False
        )

    op.create_table(
        "auth_attempts",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index("ix_events_entity", ["entity_type", "entity_id", "id"], unique=False)

    op.create_table(
        "external_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("namespace", sa.String(length=80), nullable=False),
        sa.Column("cache_key", sa.String(length=500), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etag", sa.String(length=300), nullable=True),
        sa.Column("last_modified", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("namespace", "cache_key", name="uq_external_cache"),
    )
    with op.batch_alter_table("external_cache", schema=None) as batch_op:
        batch_op.create_index("ix_external_cache_expiry", ["expires_at"], unique=False)

    op.create_table(
        "scan_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("scanned_files", sa.Integer(), nullable=False),
        sa.Column("changed_files", sa.Integer(), nullable=False),
        sa.Column("missing_files", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("status IN ('running','completed','failed')", name="ck_scan_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation"),
    )
    op.create_table(
        "service_heartbeats",
        sa.Column("service", sa.String(length=32), nullable=False),
        sa.Column("service_version", sa.String(length=64), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_work_count", sa.Integer(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("service"),
    )
    op.create_table(
        "service_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued','running','completed','retry_wait','failed')",
            name="ck_service_tasks_state",
        ),
        sa.CheckConstraint("target IN ('web','worker')", name="ck_service_tasks_target"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("service_tasks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_service_tasks_claim",
            ["target", "state", "available_at", "created_at"],
            unique=False,
        )
        batch_op.create_index("ix_service_tasks_lease", ["lease_expires_at"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("username_normalized", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username_normalized"),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("constraints_json", sa.Text(), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.Column("active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_conversations_user_id"), ["user_id"], unique=False)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_sessions_expires", ["absolute_expires_at", "idle_expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sessions_user_id"), ["user_id"], unique=False)

    op.create_table(
        "tracks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("album_id", sa.String(length=36), nullable=True),
        sa.Column("artist", sa.String(length=300), nullable=False),
        sa.Column("artist_normalized", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("title_normalized", sa.String(length=300), nullable=False),
        sa.Column("album", sa.String(length=300), nullable=True),
        sa.Column("album_artist", sa.String(length=300), nullable=True),
        sa.Column("genre", sa.String(length=200), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("track_total", sa.Integer(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("disc_total", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("recording_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_group_mbid", sa.String(length=36), nullable=True),
        sa.Column("version_signature", sa.String(length=300), nullable=False),
        sa.Column("filepath", sa.String(length=1200), nullable=False),
        sa.Column("is_present", sa.Boolean(), nullable=False),
        sa.Column("codec", sa.String(length=64), nullable=True),
        sa.Column("bitrate", sa.Integer(), nullable=True),
        sa.Column("file_mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_extractor", sa.String(length=40), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("scan_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["album_id"], ["albums.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filepath"),
    )
    with op.batch_alter_table("tracks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_tracks_identity",
            ["artist_normalized", "title_normalized", "version_signature", "is_present"],
            unique=False,
        )
        batch_op.create_index("ix_tracks_mbids", ["recording_mbid", "release_mbid"], unique=False)
        batch_op.create_index(
            "uq_tracks_source",
            ["source_extractor", "source_id"],
            unique=True,
            sqlite_where=sa.text("source_extractor IS NOT NULL AND source_id IS NOT NULL"),
        )

    op.create_table(
        "requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("refinement_parent_id", sa.String(length=36), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("input_kind", sa.String(length=32), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('find','add')", name="ck_requests_action"),
        sa.CheckConstraint(
            "status IN ('pending','orchestrating','preview','auto_queued','queued','degraded',"
            "'needs_clarification','refused','incomplete','failed')",
            name="ck_requests_status",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["refinement_parent_id"], ["requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_requests_user_idempotency"),
    )
    with op.batch_alter_table("requests", schema=None) as batch_op:
        batch_op.create_index(
            "ix_requests_conversation_created", ["conversation_id", "created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_requests_conversation_id"), ["conversation_id"], unique=False
        )
        batch_op.create_index(
            "ix_requests_orchestration", ["status", "lease_expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_requests_user_id"), ["user_id"], unique=False)

    op.create_table(
        "openai_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("response_id", sa.String(length=100), nullable=True),
        sa.Column("provider_request_id", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("web_search_count", sa.Integer(), nullable=False),
        sa.Column("web_search_context", sa.String(length=20), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("service_tier", sa.String(length=40), nullable=True),
        sa.Column("pricing_snapshot_json", sa.Text(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("openai_calls", schema=None) as batch_op:
        batch_op.create_index(
            "ix_openai_calls_created_model", ["created_at", "model"], unique=False
        )

    op.create_table(
        "request_tracks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("artist", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("album", sa.String(length=300), nullable=True),
        sa.Column("album_artist", sa.String(length=300), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("recording_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_mbid", sa.String(length=36), nullable=True),
        sa.Column("release_group_mbid", sa.String(length=36), nullable=True),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("source_extractor", sa.String(length=40), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("version_signature", sa.String(length=300), nullable=False),
        sa.Column("rationale", sa.String(length=1000), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("duplicate_status", sa.String(length=16), nullable=False),
        sa.Column("duplicate_track_id", sa.String(length=36), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_confidence", sa.Float(), nullable=True),
        sa.Column("metadata_provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("duplicate_status IN ('none','owned','possible')", name="ck_track_dup"),
        sa.ForeignKeyConstraint(["duplicate_track_id"], ["tracks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "ordinal", name="uq_request_tracks_ordinal"),
    )
    with op.batch_alter_table("request_tracks", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_request_tracks_recording_mbid"), ["recording_mbid"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_request_tracks_release_group_mbid"), ["release_group_mbid"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_request_tracks_release_mbid"), ["release_mbid"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_request_tracks_request_id"), ["request_id"], unique=False
        )

    op.create_table(
        "download_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_track_id", sa.String(length=36), nullable=False),
        sa.Column("approved_snapshot_json", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("source_extractor", sa.String(length=40), nullable=True),
        sa.Column("source_id", sa.String(length=100), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("final_track_id", sa.String(length=36), nullable=True),
        sa.Column("final_relative_path", sa.String(length=1200), nullable=True),
        sa.Column("final_sha256", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued','active','retry_wait','needs_review','waiting_for_space',"
            "'cancel_requested','cancelled','failed','completed')",
            name="ck_download_jobs_status",
        ),
        sa.ForeignKeyConstraint(["final_track_id"], ["tracks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_track_id"], ["request_tracks.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_track_id", name="uq_download_jobs_request_track"),
    )
    with op.batch_alter_table("download_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_download_jobs_claim",
            ["status", "available_at", "priority", "created_at"],
            unique=False,
        )
        batch_op.create_index("ix_download_jobs_lease", ["lease_expires_at"], unique=False)
        batch_op.create_index(
            "uq_download_jobs_active_dedup",
            ["dedup_key"],
            unique=True,
            sqlite_where=sa.text("status NOT IN ('cancelled','failed','completed')"),
        )

    op.create_table(
        "openai_tool_calls",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("openai_call_id", sa.String(length=36), nullable=False),
        sa.Column("provider_call_id", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("tool_kind", sa.String(length=20), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("result_summary_json", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["openai_call_id"], ["openai_calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("openai_call_id", "provider_call_id", name="uq_openai_tool_call"),
    )
    with op.batch_alter_table("openai_tool_calls", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_openai_tool_calls_openai_call_id"), ["openai_call_id"], unique=False
        )

    op.create_table(
        "job_review_options",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("provider_payload_json", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "kind", "rank", name="uq_job_review_rank"),
    )
    with op.batch_alter_table("job_review_options", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_job_review_options_job_id"), ["job_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("job_review_options", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_review_options_job_id"))

    op.drop_table("job_review_options")
    with op.batch_alter_table("openai_tool_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_openai_tool_calls_openai_call_id"))

    op.drop_table("openai_tool_calls")
    with op.batch_alter_table("download_jobs", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_download_jobs_active_dedup",
            sqlite_where=sa.text("status NOT IN ('cancelled','failed','completed')"),
        )
        batch_op.drop_index("ix_download_jobs_lease")
        batch_op.drop_index("ix_download_jobs_claim")

    op.drop_table("download_jobs")
    with op.batch_alter_table("request_tracks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_request_tracks_request_id"))
        batch_op.drop_index(batch_op.f("ix_request_tracks_release_mbid"))
        batch_op.drop_index(batch_op.f("ix_request_tracks_release_group_mbid"))
        batch_op.drop_index(batch_op.f("ix_request_tracks_recording_mbid"))

    op.drop_table("request_tracks")
    with op.batch_alter_table("openai_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_openai_calls_created_model")

    op.drop_table("openai_calls")
    with op.batch_alter_table("requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_requests_user_id"))
        batch_op.drop_index("ix_requests_orchestration")
        batch_op.drop_index(batch_op.f("ix_requests_conversation_id"))
        batch_op.drop_index("ix_requests_conversation_created")

    op.drop_table("requests")
    with op.batch_alter_table("tracks", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_tracks_source",
            sqlite_where=sa.text("source_extractor IS NOT NULL AND source_id IS NOT NULL"),
        )
        batch_op.drop_index("ix_tracks_mbids")
        batch_op.drop_index("ix_tracks_identity")

    op.drop_table("tracks")
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sessions_user_id"))
        batch_op.drop_index("ix_sessions_expires")

    op.drop_table("sessions")
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_conversations_user_id"))

    op.drop_table("conversations")
    op.drop_table("users")
    with op.batch_alter_table("service_tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_service_tasks_lease")
        batch_op.drop_index("ix_service_tasks_claim")

    op.drop_table("service_tasks")
    op.drop_table("service_heartbeats")
    op.drop_table("scan_runs")
    with op.batch_alter_table("external_cache", schema=None) as batch_op:
        batch_op.drop_index("ix_external_cache_expiry")

    op.drop_table("external_cache")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index("ix_events_entity")

    op.drop_table("events")
    op.drop_table("auth_attempts")
    with op.batch_alter_table("artwork_cache", schema=None) as batch_op:
        batch_op.drop_index("ix_artwork_mbids")
        batch_op.drop_index(batch_op.f("ix_artwork_cache_expires_at"))

    op.drop_table("artwork_cache")
    with op.batch_alter_table("albums", schema=None) as batch_op:
        batch_op.drop_index("ix_albums_mbids")
        batch_op.drop_index(batch_op.f("ix_albums_artist_normalized"))

    op.drop_table("albums")
