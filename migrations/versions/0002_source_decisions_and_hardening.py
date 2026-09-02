"""Durable source decisions and production-hardening state.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_SOURCE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_LEGACY_EXECUTABLE_SOURCES = {
    "youtube": (("youtube",), ("youtube.com", "youtu.be")),
    "soundcloud": (("soundcloud",), ("soundcloud.com",)),
    "bandcamp": (("bandcamp", "bandcamp:track"), ("bandcamp.com",)),
}


def uuid7() -> str:
    """Generate migration identifiers without importing mutable application code."""

    native = getattr(uuid, "uuid7", None)
    if native is not None:
        return str(native())
    timestamp_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    random_bits = secrets.randbits(74)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    return str(uuid.UUID(int=value))


def upgrade() -> None:
    op.create_table(
        "evidence_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("request_track_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=True),
        sa.Column("provider_item_id", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("sanitized_metadata_json", sa.Text(), nullable=False),
        sa.Column("negative_reason", sa.String(length=120), nullable=True),
        sa.Column("negative_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','available','unsupported','rejected','expired')",
            name="ck_evidence_references_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_track_id"], ["request_tracks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evidence_references_job_id", "evidence_references", ["job_id"], unique=False
    )
    op.create_index(
        "ix_evidence_references_request_id",
        "evidence_references",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_references_request_track_id",
        "evidence_references",
        ["request_track_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_request_track",
        "evidence_references",
        ["request_track_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_negative_until",
        "evidence_references",
        ["negative_until"],
        unique=False,
    )

    op.create_table(
        "source_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("evidence_id", sa.String(length=36), nullable=True),
        sa.Column("request_track_id", sa.String(length=36), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("extractor", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("acquisition_url", sa.String(length=2048), nullable=True),
        sa.Column("provider_title", sa.String(length=500), nullable=False),
        sa.Column("provider_artist", sa.String(length=300), nullable=True),
        sa.Column("uploader", sa.String(length=300), nullable=True),
        sa.Column("uploader_relationship", sa.String(length=24), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("version_signature", sa.String(length=300), nullable=False),
        sa.Column("group_key", sa.String(length=500), nullable=False),
        sa.Column("local_score", sa.Float(), nullable=False),
        sa.Column("policy_status", sa.String(length=20), nullable=False),
        sa.Column("probe_status", sa.String(length=20), nullable=False),
        sa.Column("contradictions_json", sa.Text(), nullable=False),
        sa.Column("sanitized_metadata_json", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("superseded_by_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "policy_status IN ('pending','allowed','rejected','exhausted')",
            name="ck_source_candidates_policy_status",
        ),
        sa.CheckConstraint(
            "probe_status IN ('pending','valid','invalid','failed')",
            name="ck_source_candidates_probe_status",
        ),
        sa.CheckConstraint(
            "uploader_relationship IN ('official_artist','official_label','topic',"
            "'distributor','third_party','unknown')",
            name="ck_source_candidates_uploader_relationship",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["evidence_references.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_track_id"], ["request_tracks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"], ["source_candidates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_track_id",
            "provider",
            "extractor",
            "source_id",
            name="uq_source_candidates_identity",
        ),
    )
    for column in ("evidence_id", "request_track_id", "job_id"):
        op.create_index(
            f"ix_source_candidates_{column}", "source_candidates", [column], unique=False
        )
    op.create_index(
        "ix_source_candidates_job_rank",
        "source_candidates",
        ["job_id", "policy_status", "local_score"],
        unique=False,
    )
    op.create_index(
        "ix_source_candidates_evidence",
        "source_candidates",
        ["evidence_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "job_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("candidate_set_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("selected_payload_json", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=20), nullable=True),
        sa.Column("openai_call_id", sa.String(length=36), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("local_confidence", sa.Float(), nullable=True),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("contradictions_json", sa.Text(), nullable=False),
        sa.Column("reason_codes_json", sa.Text(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('recording_version','acquisition_source','canonical_metadata',"
            "'possible_duplicate')",
            name="ck_job_decisions_category",
        ),
        sa.CheckConstraint(
            "state IN ('pending','selected','superseded','rejected')",
            name="ck_job_decisions_state",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by IN ('deterministic','openai','user','migration')",
            name="ck_job_decisions_decided_by",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["openai_call_id"], ["openai_calls.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "category",
            "candidate_set_fingerprint",
            "revision",
            name="uq_job_decisions_fingerprint_revision",
        ),
    )
    op.create_index("ix_job_decisions_job_id", "job_decisions", ["job_id"], unique=False)
    op.create_index(
        "ix_job_decisions_pending",
        "job_decisions",
        ["job_id", "state", "category"],
        unique=False,
    )

    op.create_table(
        "job_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("relative_path", sa.String(length=1200), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("generation_token", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('creating','ready','published','removed','invalid')",
            name="ck_job_artifacts_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["download_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "kind", "relative_path", name="uq_job_artifacts_path"),
    )
    op.create_index("ix_job_artifacts_job_id", "job_artifacts", ["job_id"], unique=False)
    op.create_index(
        "ix_job_artifacts_recovery",
        "job_artifacts",
        ["job_id", "status", "stage"],
        unique=False,
    )

    # These are additive changes. Use SQLite's native ADD COLUMN rather than a
    # batch table recreation: request_tracks/download_jobs/openai_calls are all
    # referenced by populated child tables, and recreating a referenced table
    # fails correctly while foreign-key enforcement is enabled.
    op.add_column("request_tracks", sa.Column("suggested_recording_mbid", sa.String(36)))
    op.add_column("request_tracks", sa.Column("suggested_release_mbid", sa.String(36)))
    op.add_column("request_tracks", sa.Column("suggested_release_group_mbid", sa.String(36)))
    op.add_column(
        "request_tracks",
        sa.Column(
            "canonical_identity_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("download_jobs", sa.Column("active_source_candidate_id", sa.String(36)))
    op.add_column(
        "download_jobs",
        sa.Column("source_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "download_jobs",
        sa.Column("decision_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "download_jobs",
        sa.Column("review_round_count", sa.Integer(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("job_review_options") as batch_op:
        batch_op.drop_constraint("uq_job_review_rank", type_="unique")
        batch_op.add_column(sa.Column("decision_id", sa.String(36)))
        batch_op.add_column(sa.Column("option_key", sa.String(160)))
        batch_op.add_column(sa.Column("fingerprint", sa.String(64)))
        batch_op.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(
            sa.Column(
                "materially_different", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
        batch_op.create_foreign_key(
            "fk_job_review_options_decision_id_job_decisions",
            "job_decisions",
            ["decision_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_job_review_options_decision_id", ["decision_id"])
        batch_op.create_unique_constraint("uq_job_review_decision_rank", ["decision_id", "rank"])
    op.add_column("openai_calls", sa.Column("exception_class", sa.String(120)))
    op.add_column("openai_calls", sa.Column("http_status", sa.Integer()))
    op.add_column("openai_calls", sa.Column("provider_error_code", sa.String(120)))
    op.add_column("openai_calls", sa.Column("provider_error_parameter", sa.String(120)))
    op.add_column("openai_calls", sa.Column("application_call_id", sa.String(64)))
    op.add_column("openai_calls", sa.Column("failure_phase", sa.String(40)))
    op.add_column("openai_calls", sa.Column("retryable", sa.Boolean()))

    _backfill_legacy_state()


def _backfill_legacy_state() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)
    legacy_options = list(
        connection.execute(
            sa.text(
                "SELECT jro.id, jro.job_id, jro.kind, jro.rank, "
                "jro.provider_payload_json, jro.score, jro.selected_at, "
                "dj.request_track_id, dj.status AS job_status, "
                "rt.request_id, rt.artist, rt.title "
                "FROM job_review_options jro "
                "JOIN download_jobs dj ON dj.id=jro.job_id "
                "JOIN request_tracks rt ON rt.id=dj.request_track_id "
                "ORDER BY jro.job_id, jro.kind, jro.rank, jro.id"
            )
        ).mappings()
    )
    selected_metadata_by_track = _selected_legacy_metadata(legacy_options)
    tracks = connection.execute(
        sa.text(
            "SELECT id, request_id, artist, title, recording_mbid, release_mbid, "
            "release_group_mbid, source_url, source_extractor, source_id, "
            "metadata_provenance_json FROM request_tracks"
        )
    ).mappings()
    evidence_by_track: dict[str, str] = {}
    candidate_by_track: dict[str, str] = {}
    automatic_metadata_by_track: dict[str, dict[str, object]] = {}
    for track in tracks:
        track_id = str(track["id"])
        try:
            provenance = json.loads(track["metadata_provenance_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            provenance = {}
        if not isinstance(provenance, dict):
            provenance = {}
        automatic_recording_mbid = _mbid(track["recording_mbid"])
        automatically_verified = (
            bool(provenance.get("automatic_association"))
            and provenance.get("source") == "musicbrainz_search_recordings"
            and automatic_recording_mbid is not None
            and _mbid(provenance.get("recording_mbid")) == automatic_recording_mbid
        )
        selected_metadata = selected_metadata_by_track.get(track_id)
        selected_recording = (
            _mbid(selected_metadata.get("recording_mbid"))
            if selected_metadata is not None
            else None
        )
        user_verified = selected_recording is not None
        verified = automatically_verified or user_verified
        recording_mbid = (
            selected_recording
            if user_verified
            else automatic_recording_mbid
            if automatically_verified
            else None
        )
        release_mbid = (
            _mbid(selected_metadata.get("release_mbid"))
            if user_verified and selected_metadata is not None
            else _mbid(track["release_mbid"])
            if automatically_verified
            else None
        )
        release_group_mbid = (
            _mbid(selected_metadata.get("release_group_mbid"))
            if user_verified and selected_metadata is not None
            else _mbid(track["release_group_mbid"])
            if automatically_verified
            else None
        )
        if user_verified:
            provenance = {
                "automatic_association": False,
                "source": "user_confirmed_legacy_review",
                "decided_by": "migration",
                "recording_mbid": recording_mbid,
                "release_mbid": release_mbid,
                "release_group_mbid": release_group_mbid,
            }
        elif automatically_verified:
            automatic_metadata_by_track[track_id] = {
                "recording_mbid": recording_mbid,
                "release_mbid": release_mbid,
                "release_group_mbid": release_group_mbid,
                "metadata_provenance": dict(provenance),
            }
        connection.execute(
            sa.text(
                "UPDATE request_tracks SET canonical_identity_verified=:verified, "
                "suggested_recording_mbid=:suggested_recording, "
                "suggested_release_mbid=:suggested_release, "
                "suggested_release_group_mbid=:suggested_group, "
                "recording_mbid=:recording, release_mbid=:release, "
                "release_group_mbid=:release_group, "
                "metadata_provenance_json=:provenance WHERE id=:id"
            ),
            {
                "id": track_id,
                "verified": verified,
                "suggested_recording": (
                    None
                    if automatically_verified or track["recording_mbid"] == recording_mbid
                    else track["recording_mbid"]
                ),
                "suggested_release": (
                    None
                    if automatically_verified or track["release_mbid"] == release_mbid
                    else track["release_mbid"]
                ),
                "suggested_group": (
                    None
                    if automatically_verified or track["release_group_mbid"] == release_group_mbid
                    else track["release_group_mbid"]
                ),
                "recording": recording_mbid,
                "release": release_mbid,
                "release_group": release_group_mbid,
                "provenance": json.dumps(provenance, separators=(",", ":")),
            },
        )
        source_url = track["source_url"]
        extractor = str(track["source_extractor"] or "").strip().casefold()
        source_id = str(track["source_id"] or "").strip()
        if source_url:
            evidence_id = uuid7()
            evidence_by_track[track_id] = evidence_id
            provider = _provider_for_legacy(str(source_url), extractor)
            connection.execute(
                sa.text(
                    "INSERT INTO evidence_references "
                    "(id,request_id,request_track_id,job_id,provider,evidence_kind,"
                    "canonical_url,provider_item_id,status,sanitized_metadata_json,"
                    "negative_reason,negative_until,created_at,updated_at) VALUES "
                    "(:id,:request_id,:track_id,NULL,:provider,'legacy_url',:url,:item_id,"
                    "'available',:metadata,NULL,NULL,:created,:updated)"
                ),
                {
                    "id": evidence_id,
                    "request_id": track["request_id"],
                    "track_id": track_id,
                    "provider": provider,
                    "url": source_url,
                    "item_id": source_id or None,
                    "metadata": '{"legacy":true,"requires_revalidation":true}',
                    "created": now,
                    "updated": now,
                },
            )
        if extractor and source_id:
            candidate_id = uuid7()
            candidate_by_track[track_id] = candidate_id
            connection.execute(
                sa.text(
                    "INSERT INTO source_candidates "
                    "(id,evidence_id,request_track_id,job_id,provider,extractor,source_id,"
                    "acquisition_url,provider_title,provider_artist,uploader,"
                    "uploader_relationship,duration_seconds,version_signature,group_key,"
                    "local_score,policy_status,probe_status,contradictions_json,"
                    "sanitized_metadata_json,attempted_at,failure_code,superseded_by_id,"
                    "created_at,updated_at) VALUES "
                    "(:id,:evidence_id,:track_id,NULL,:provider,:extractor,:source_id,NULL,"
                    ":title,:artist,NULL,'unknown',NULL,'studio',:group_key,0.0,'pending',"
                    "'pending','[]',:metadata,NULL,NULL,NULL,:created,:updated)"
                ),
                {
                    "id": candidate_id,
                    "evidence_id": evidence_by_track.get(track_id),
                    "track_id": track_id,
                    "provider": _provider_for_legacy(str(source_url or ""), extractor),
                    "extractor": extractor,
                    "source_id": source_id,
                    "title": str(track["title"] or "")[:500],
                    "artist": str(track["artist"] or "")[:300] or None,
                    "group_key": f"legacy:{extractor}:{source_id}"[:500],
                    "metadata": '{"legacy":true,"requires_revalidation":true}',
                    "created": now,
                    "updated": now,
                },
            )
        connection.execute(
            sa.text(
                "UPDATE request_tracks SET source_url=NULL, source_extractor=NULL, "
                "source_id=NULL WHERE id=:id"
            ),
            {"id": track_id},
        )

    jobs = connection.execute(
        sa.text("SELECT id, request_track_id, approved_snapshot_json, status FROM download_jobs")
    ).mappings()
    for job in jobs:
        track_id = str(job["request_track_id"])
        evidence_id = evidence_by_track.get(track_id)
        candidate_id = candidate_by_track.get(track_id)
        if evidence_id:
            connection.execute(
                sa.text("UPDATE evidence_references SET job_id=:job_id WHERE id=:id"),
                {"job_id": job["id"], "id": evidence_id},
            )
        if candidate_id:
            connection.execute(
                sa.text("UPDATE source_candidates SET job_id=:job_id WHERE id=:id"),
                {"job_id": job["id"], "id": candidate_id},
            )
        try:
            snapshot = json.loads(job["approved_snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(snapshot, dict):
            continue
        verified = False
        automatic_metadata = automatic_metadata_by_track.get(track_id)
        if automatic_metadata is not None:
            verified = True
            snapshot["recording_mbid"] = automatic_metadata["recording_mbid"]
            for name in ("release_mbid", "release_group_mbid"):
                value = automatic_metadata.get(name)
                if value is not None:
                    snapshot[name] = value
                else:
                    snapshot.pop(name, None)
            snapshot["canonical_identity_verified"] = True
            snapshot["metadata_provenance"] = automatic_metadata["metadata_provenance"]
        selected_metadata = selected_metadata_by_track.get(track_id)
        selected_recording = (
            _mbid(selected_metadata.get("recording_mbid"))
            if selected_metadata is not None
            else None
        )
        if selected_recording is not None and selected_metadata is not None:
            verified = True
            snapshot["recording_mbid"] = selected_recording
            for name in ("release_mbid", "release_group_mbid"):
                value = _mbid(selected_metadata.get(name))
                if value is not None:
                    snapshot[name] = value
                else:
                    snapshot.pop(name, None)
            snapshot["canonical_identity_verified"] = True
            snapshot["metadata_provenance"] = {
                "automatic_association": False,
                "source": "user_confirmed_legacy_review",
                "decided_by": "migration",
            }
        if not verified:
            snapshot["canonical_identity_verified"] = False
            for name in ("recording_mbid", "release_mbid", "release_group_mbid"):
                value = snapshot.pop(name, None)
                if value:
                    snapshot[f"suggested_{name}"] = value
        snapshot.pop("source_url", None)
        snapshot.pop("source_extractor", None)
        snapshot.pop("source_id", None)
        if evidence_id:
            snapshot["evidence_reference_id"] = evidence_id
        if candidate_id:
            snapshot["legacy_source_candidate_id"] = candidate_id
        connection.execute(
            sa.text("UPDATE download_jobs SET approved_snapshot_json=:snapshot WHERE id=:id"),
            {
                "id": job["id"],
                "snapshot": json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            },
        )

    _backfill_legacy_review_decisions(
        connection,
        legacy_options,
        now=now,
    )


def _selected_legacy_metadata(
    options: list[sa.RowMapping],
) -> dict[str, dict[str, object]]:
    selected_options: dict[str, list[dict[str, object]]] = {}
    for option in options:
        if option["selected_at"] is None or _decision_category(str(option["kind"])) != (
            "canonical_metadata"
        ):
            continue
        payload = _json_object(option["provider_payload_json"])
        if (
            payload is not None
            and _selection_is_reconstructable("canonical_metadata", payload)
            and _mbid(payload.get("recording_mbid")) is not None
        ):
            selected_options.setdefault(str(option["request_track_id"]), []).append(payload)
    # A corrupt/hand-edited legacy database can contain multiple selections.
    # Never pick an arbitrary winner and accidentally grant canonical authority.
    return {
        track_id: payloads[0]
        for track_id, payloads in selected_options.items()
        if len(payloads) == 1
    }


def _backfill_legacy_review_decisions(
    connection: sa.Connection,
    options: list[sa.RowMapping],
    *,
    now: datetime,
) -> None:
    grouped: dict[tuple[str, str], list[sa.RowMapping]] = {}
    for option in options:
        key = (str(option["job_id"]), _decision_category(str(option["kind"])))
        grouped.setdefault(key, []).append(option)

    revisions: dict[str, int] = {}
    pending_counts: dict[str, int] = {}
    for (job_id, category), rows in grouped.items():
        transformed: list[tuple[sa.RowMapping, dict[str, object]]] = []
        for row in rows:
            payload = _json_object(row["provider_payload_json"])
            if payload is None:
                payload = _invalid_legacy_payload(category, row)
            else:
                try:
                    payload = _stable_payload_object(payload)
                except ValueError:
                    payload = _invalid_legacy_payload(category, row)
            if category == "acquisition_source":
                durable_source = _backfill_legacy_source_option(connection, row, payload, now)
                if durable_source is not None:
                    payload = {**payload, **durable_source, "legacy_requires_revalidation": True}
            transformed.append((row, _stable_payload_object(payload)))

        selected_items = [item for item in transformed if item[0]["selected_at"] is not None]
        selected = selected_items[0] if len(selected_items) == 1 else None
        selected_payload = selected[1] if selected is not None else None
        reconstructable = selected_payload is not None and _selection_is_reconstructable(
            category, selected_payload
        )
        state = "selected" if reconstructable else "pending"
        if state == "pending":
            pending_counts[job_id] = pending_counts.get(job_id, 0) + 1

        revision = revisions.get(job_id, 0) + 1
        revisions[job_id] = revision
        decision_id = uuid7()
        candidate_payloads = [payload for _, payload in transformed]
        fingerprint = _candidate_set_fingerprint(category, candidate_payloads)
        connection.execute(
            sa.text(
                "INSERT INTO job_decisions "
                "(id,job_id,category,candidate_set_fingerprint,revision,state,"
                "selected_payload_json,decided_by,openai_call_id,prompt_version,"
                "local_confidence,model_confidence,contradictions_json,reason_codes_json,"
                "round_number,created_at,decided_at) VALUES "
                "(:id,:job_id,:category,:fingerprint,:revision,:state,:selected_payload,"
                ":decided_by,NULL,NULL,NULL,NULL,'[]',:reason_codes,:round_number,"
                ":created_at,:decided_at)"
            ),
            {
                "id": decision_id,
                "job_id": job_id,
                "category": category,
                "fingerprint": fingerprint,
                "revision": revision,
                "state": state,
                "selected_payload": (
                    json.dumps(selected_payload, ensure_ascii=False, separators=(",", ":"))
                    if reconstructable
                    else None
                ),
                "decided_by": "migration" if reconstructable else None,
                "reason_codes": json.dumps(
                    [
                        (
                            "legacy_review_selection_migrated"
                            if reconstructable
                            else "legacy_review_conflicting_selections"
                            if len(selected_items) > 1
                            else "legacy_review_requires_revalidation"
                        )
                    ],
                    separators=(",", ":"),
                ),
                "round_number": 0 if reconstructable else 1,
                "created_at": now,
                "decided_at": now if reconstructable else None,
            },
        )
        for rank, (row, payload) in enumerate(transformed, start=1):
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            is_selected = (
                reconstructable and selected is not None and row["id"] == selected[0]["id"]
            )
            connection.execute(
                sa.text(
                    "UPDATE job_review_options SET decision_id=:decision_id, kind=:kind, "
                    "rank=:rank, option_key=:option_key, fingerprint=:fingerprint, "
                    "revision=:revision, materially_different=1, "
                    "provider_payload_json=:payload, selected_at=:selected_at WHERE id=:id"
                ),
                {
                    "id": row["id"],
                    "decision_id": decision_id,
                    "kind": category,
                    "rank": rank,
                    "option_key": _option_key(payload),
                    "fingerprint": _payload_fingerprint(payload),
                    "revision": revision,
                    "payload": payload_json,
                    "selected_at": row["selected_at"] if is_selected else None,
                },
            )
        if reconstructable and category == "acquisition_source" and selected_payload is not None:
            connection.execute(
                sa.text(
                    "UPDATE download_jobs SET active_source_candidate_id=NULL, "
                    "approved_snapshot_json=json_set(approved_snapshot_json, "
                    "'$.legacy_source_candidate_id', :candidate_id, "
                    "'$.evidence_reference_id', :evidence_id) WHERE id=:job_id"
                ),
                {
                    "job_id": job_id,
                    "candidate_id": selected_payload["source_candidate_id"],
                    "evidence_id": selected_payload["evidence_reference_id"],
                },
            )

    for job_id, revision in revisions.items():
        connection.execute(
            sa.text(
                "UPDATE download_jobs SET decision_revision=:revision, "
                "review_round_count=:rounds WHERE id=:job_id"
            ),
            {
                "job_id": job_id,
                "revision": revision,
                "rounds": pending_counts.get(job_id, 0),
            },
        )


def _backfill_legacy_source_option(
    connection: sa.Connection,
    row: sa.RowMapping,
    payload: dict[str, object],
    now: datetime,
) -> dict[str, object] | None:
    details = _legacy_source_details(payload)
    if details is None:
        return None
    url, provider, extractor, source_id = details
    track_id = str(row["request_track_id"])
    evidence_id = connection.scalar(
        sa.text(
            "SELECT id FROM evidence_references WHERE request_track_id=:track_id "
            "AND provider=:provider AND canonical_url=:url ORDER BY created_at LIMIT 1"
        ),
        {"track_id": track_id, "provider": provider, "url": url},
    )
    if evidence_id is None:
        evidence_id = uuid7()
        connection.execute(
            sa.text(
                "INSERT INTO evidence_references "
                "(id,request_id,request_track_id,job_id,provider,evidence_kind,canonical_url,"
                "provider_item_id,status,sanitized_metadata_json,negative_reason,"
                "negative_until,created_at,updated_at) VALUES "
                "(:id,:request_id,:track_id,:job_id,:provider,'legacy_selected_review',"
                ":url,:source_id,'available',:metadata,NULL,NULL,:created,:updated)"
            ),
            {
                "id": evidence_id,
                "request_id": row["request_id"],
                "track_id": track_id,
                "job_id": row["job_id"],
                "provider": provider,
                "url": url,
                "source_id": source_id,
                "metadata": '{"legacy":true,"requires_revalidation":true}',
                "created": now,
                "updated": now,
            },
        )
    candidate_id = connection.scalar(
        sa.text(
            "SELECT id FROM source_candidates WHERE request_track_id=:track_id "
            "AND provider=:provider AND extractor=:extractor AND source_id=:source_id"
        ),
        {
            "track_id": track_id,
            "provider": provider,
            "extractor": extractor,
            "source_id": source_id,
        },
    )
    if candidate_id is None:
        candidate_id = uuid7()
        score = row["score"]
        local_score = (
            min(1.0, max(0.0, float(score)))
            if isinstance(score, (int, float)) and math.isfinite(float(score))
            else 0.0
        )
        duration = payload.get("duration_seconds")
        duration_seconds = (
            float(duration)
            if isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(float(duration))
            else None
        )
        connection.execute(
            sa.text(
                "INSERT INTO source_candidates "
                "(id,evidence_id,request_track_id,job_id,provider,extractor,source_id,"
                "acquisition_url,provider_title,provider_artist,uploader,"
                "uploader_relationship,duration_seconds,version_signature,group_key,"
                "local_score,policy_status,probe_status,contradictions_json,"
                "sanitized_metadata_json,attempted_at,failure_code,superseded_by_id,"
                "created_at,updated_at) VALUES "
                "(:id,:evidence_id,:track_id,:job_id,:provider,:extractor,:source_id,NULL,"
                ":title,:artist,:uploader,'unknown',:duration,:version,:group_key,:score,"
                "'pending','pending','[]',:metadata,NULL,NULL,NULL,:created,:updated)"
            ),
            {
                "id": candidate_id,
                "evidence_id": evidence_id,
                "track_id": track_id,
                "job_id": row["job_id"],
                "provider": provider,
                "extractor": extractor,
                "source_id": source_id,
                "title": _bounded_text(payload.get("title"), 500)
                or _bounded_text(row["title"], 500)
                or "Legacy source",
                "artist": _bounded_text(payload.get("artist"), 300)
                or _bounded_text(row["artist"], 300),
                "uploader": _bounded_text(payload.get("channel"), 300)
                or _bounded_text(payload.get("uploader"), 300),
                "duration": duration_seconds,
                "version": _bounded_text(payload.get("version_signature"), 300) or "studio",
                "group_key": f"legacy:{provider}:{extractor}:{source_id}"[:500],
                "score": local_score,
                "metadata": '{"legacy":true,"requires_revalidation":true}',
                "created": now,
                "updated": now,
            },
        )
    else:
        connection.execute(
            sa.text("UPDATE source_candidates SET job_id=COALESCE(job_id,:job_id) WHERE id=:id"),
            {"id": candidate_id, "job_id": row["job_id"]},
        )
    return {
        "source_candidate_id": str(candidate_id),
        "evidence_reference_id": str(evidence_id),
    }


def _legacy_source_details(
    payload: dict[str, object],
) -> tuple[str, str, str, str] | None:
    raw_url = payload.get("url")
    extractor_value = payload.get("source_extractor")
    source_id_value = payload.get("source_id")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (raw_url, extractor_value, source_id_value)
    ):
        return None
    url = str(raw_url).strip()
    extractor = str(extractor_value).strip().casefold()
    source_id = str(source_id_value).strip()
    if len(url) > 2048 or len(extractor) > 80 or _LEGACY_SOURCE_ID.fullmatch(source_id) is None:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return None
    hostname = parsed.hostname.rstrip(".").casefold()
    provider = next(
        (
            name
            for name, (extractors, suffixes) in _LEGACY_EXECUTABLE_SOURCES.items()
            if extractor in extractors
            and any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)
        ),
        None,
    )
    if provider is None:
        return None
    return url, provider, extractor, source_id


def _selection_is_reconstructable(category: str, payload: dict[str, object]) -> bool:
    if category == "acquisition_source":
        return all(
            isinstance(payload.get(key), str) and bool(str(payload[key]).strip())
            for key in ("source_candidate_id", "evidence_reference_id")
        )
    if category == "canonical_metadata":
        return bool(_bounded_text(payload.get("artist"), 300)) and bool(
            _bounded_text(payload.get("title"), 300)
        )
    if category == "possible_duplicate":
        return bool(_bounded_text(payload.get("track_id"), 36))
    return bool(_bounded_text(payload.get("version_signature"), 300))


def _decision_category(kind: str) -> str:
    normalized = kind.strip().casefold()
    return {
        "source": "acquisition_source",
        "acquisition_source": "acquisition_source",
        "metadata": "canonical_metadata",
        "canonical_metadata": "canonical_metadata",
        "duplicate": "possible_duplicate",
        "possible_duplicate": "possible_duplicate",
        "version": "recording_version",
        "recording_version": "recording_version",
    }.get(normalized, "acquisition_source")


def _json_object(value: object) -> dict[str, object] | None:
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _invalid_legacy_payload(category: str, row: sa.RowMapping) -> dict[str, object]:
    return {
        "kind": category,
        "legacy_option_id": str(row["id"]),
        "legacy_payload_invalid": True,
    }


def _stable_payload_object(value: dict[str, object]) -> dict[str, object]:
    stable = _stable_payload(value)
    if not isinstance(stable, dict):
        raise ValueError("legacy review option is not an object")
    return stable


def _stable_payload(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("legacy review option contains a non-finite number")
        return value
    if isinstance(value, dict):
        return {
            str(key)[:160]: _stable_payload(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(key, str)
        }
    if isinstance(value, list):
        return [_stable_payload(item) for item in value]
    raise ValueError("legacy review option contains an unsupported value")


def _payload_fingerprint(value: object) -> str:
    encoded = json.dumps(
        _stable_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _candidate_set_fingerprint(category: str, options: list[dict[str, object]]) -> str:
    return _payload_fingerprint(
        {
            "category": category,
            "options": sorted(
                (_stable_payload(option) for option in options),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            ),
        }
    )


def _option_key(payload: dict[str, object]) -> str:
    for key in (
        "source_candidate_id",
        "recording_candidate_id",
        "release_candidate_id",
        "track_id",
        "source_id",
    ):
        value = _bounded_text(payload.get(key), 120)
        if value:
            return f"{key}:{value}"[:160]
    return f"sha256:{_payload_fingerprint(payload)[:96]}"


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _mbid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value.strip()))
    except (ValueError, AttributeError):
        return None


def _provider_for_legacy(url: str, extractor: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    if "soundcloud" in extractor or host.endswith("soundcloud.com"):
        return "soundcloud"
    if "bandcamp" in extractor or host.endswith("bandcamp.com"):
        return "bandcamp"
    if "youtube" in extractor or host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    return "unknown"


def downgrade() -> None:
    connection = op.get_bind()
    for row in connection.execute(
        sa.text(
            "SELECT rt.id, er.canonical_url FROM request_tracks rt "
            "LEFT JOIN evidence_references er ON er.request_track_id=rt.id "
            "AND er.evidence_kind='legacy_url'"
        )
    ).mappings():
        connection.execute(
            sa.text(
                "UPDATE request_tracks SET source_url=COALESCE(source_url,:url), "
                "recording_mbid=COALESCE(recording_mbid,suggested_recording_mbid), "
                "release_mbid=COALESCE(release_mbid,suggested_release_mbid), "
                "release_group_mbid=COALESCE(release_group_mbid,"
                "suggested_release_group_mbid) WHERE id=:id"
            ),
            {"id": row["id"], "url": row["canonical_url"]},
        )

    for column in (
        "retryable",
        "failure_phase",
        "application_call_id",
        "provider_error_parameter",
        "provider_error_code",
        "http_status",
        "exception_class",
    ):
        op.drop_column("openai_calls", column)
    with op.batch_alter_table("job_review_options") as batch_op:
        batch_op.drop_constraint("uq_job_review_decision_rank", type_="unique")
        batch_op.drop_index("ix_job_review_options_decision_id")
        batch_op.drop_constraint(
            "fk_job_review_options_decision_id_job_decisions", type_="foreignkey"
        )
        for column in (
            "materially_different",
            "revision",
            "fingerprint",
            "option_key",
            "decision_id",
        ):
            batch_op.drop_column(column)
        batch_op.create_unique_constraint("uq_job_review_rank", ["job_id", "kind", "rank"])
    for column in (
        "review_round_count",
        "decision_revision",
        "source_attempt_count",
        "active_source_candidate_id",
    ):
        op.drop_column("download_jobs", column)
    for column in (
        "canonical_identity_verified",
        "suggested_release_group_mbid",
        "suggested_release_mbid",
        "suggested_recording_mbid",
    ):
        op.drop_column("request_tracks", column)
    op.drop_table("job_artifacts")
    op.drop_table("job_decisions")
    op.drop_table("source_candidates")
    op.drop_table("evidence_references")
