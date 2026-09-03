from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import CheckConstraint, Index, inspect, text

from app.cli import migrate, validate
from app.config import Settings
from app.db.engine import create_database_engine, current_revision
from app.db.models import Base
from app.main import create_app
from app.repositories.auth import AuthRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_DIRECTORY = REPO_ROOT / "migrations" / "versions"


def _upgrade(database_path: Path, revision: str) -> None:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.upgrade(config, revision)


def _downgrade(database_path: Path, revision: str) -> None:
    config = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")
    command.downgrade(config, revision)


def _seed_legacy_review_state(database_path: Path) -> dict[str, str]:
    identifiers = {
        "user": "00000000-0000-0000-0000-000000000001",
        "conversation": "00000000-0000-0000-0000-000000000002",
        "request": "00000000-0000-0000-0000-000000000003",
        "metadata_track": "00000000-0000-0000-0000-000000000011",
        "source_track": "00000000-0000-0000-0000-000000000012",
        "incomplete_track": "00000000-0000-0000-0000-000000000013",
        "automatic_track": "00000000-0000-0000-0000-000000000014",
        "metadata_job": "00000000-0000-0000-0000-000000000021",
        "source_job": "00000000-0000-0000-0000-000000000022",
        "incomplete_job": "00000000-0000-0000-0000-000000000023",
        "automatic_job": "00000000-0000-0000-0000-000000000024",
        "metadata_option": "00000000-0000-0000-0000-000000000031",
        "metadata_alternative": "00000000-0000-0000-0000-000000000032",
        "source_option": "00000000-0000-0000-0000-000000000033",
        "incomplete_option": "00000000-0000-0000-0000-000000000034",
        "unsafe_source_option": "00000000-0000-0000-0000-000000000035",
    }
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    recording_mbid = "11111111-1111-4111-8111-111111111111"
    release_mbid = "22222222-2222-4222-8222-222222222222"
    release_group_mbid = "33333333-3333-4333-8333-333333333333"
    automatic_recording_mbid = "44444444-4444-4444-8444-444444444444"
    automatic_release_mbid = "55555555-5555-4555-8555-555555555555"
    automatic_release_group_mbid = "66666666-6666-4666-8666-666666666666"
    identifiers.update(
        recording_mbid=recording_mbid,
        release_mbid=release_mbid,
        release_group_mbid=release_group_mbid,
        automatic_recording_mbid=automatic_recording_mbid,
        automatic_release_mbid=automatic_release_mbid,
        automatic_release_group_mbid=automatic_release_group_mbid,
    )
    engine = create_database_engine(Settings(environment="test", database_path=database_path))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(id,username,username_normalized,password_hash,is_active,created_at,"
                    "last_login_at) VALUES "
                    "(:id,'admin','admin','legacy-hash',1,:created_at,NULL)"
                ),
                {"id": identifiers["user"], "created_at": created_at},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id,user_id,title,constraints_json,turn_count,active_at,archived_at,"
                    "created_at,updated_at) VALUES "
                    "(:id,:user_id,'Legacy request','{}',1,:created_at,NULL,:created_at,"
                    ":created_at)"
                ),
                {
                    "id": identifiers["conversation"],
                    "user_id": identifiers["user"],
                    "created_at": created_at,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO requests "
                    "(id,user_id,conversation_id,refinement_parent_id,raw_text,action,"
                    "input_kind,requested_count,status,prompt_version,discovered_count,"
                    "selected_count,warning_count,idempotency_key,lease_token,"
                    "lease_expires_at,error_code,error_message,created_at,updated_at) VALUES "
                    "(:id,:user_id,:conversation_id,NULL,'add legacy fixtures','add','text',4,"
                    "'queued','orchestrator_v1',4,4,0,'legacy-migration-fixture',NULL,NULL,"
                    "NULL,NULL,:created_at,:created_at)"
                ),
                {
                    "id": identifiers["request"],
                    "user_id": identifiers["user"],
                    "conversation_id": identifiers["conversation"],
                    "created_at": created_at,
                },
            )
            track_rows = []
            for ordinal, key in enumerate(
                ("metadata_track", "source_track", "incomplete_track", "automatic_track"),
                start=1,
            ):
                is_source = key == "source_track"
                is_automatic = key == "automatic_track"
                track_rows.append(
                    {
                        "id": identifiers[key],
                        "request_id": identifiers["request"],
                        "ordinal": ordinal,
                        "artist": "Coldplay",
                        "title": "Yellow",
                        "album": "Legacy Album",
                        "recording_mbid": (
                            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                            if key == "metadata_track"
                            else automatic_recording_mbid
                            if is_automatic
                            else None
                        ),
                        "release_mbid": automatic_release_mbid if is_automatic else None,
                        "release_group_mbid": (
                            automatic_release_group_mbid if is_automatic else None
                        ),
                        "source_url": (
                            "https://www.youtube.com/watch?v=legacy123" if is_source else None
                        ),
                        "source_extractor": "youtube" if is_source else None,
                        "source_id": "legacy123" if is_source else None,
                        "metadata_provenance": json.dumps(
                            {
                                "automatic_association": True,
                                "source": "musicbrainz_search_recordings",
                                "recording_mbid": automatic_recording_mbid,
                                "release_mbid": automatic_release_mbid,
                                "score": 97.0,
                            },
                            separators=(",", ":"),
                        )
                        if is_automatic
                        else "{}",
                        "created_at": created_at,
                    }
                )
            connection.execute(
                text(
                    "INSERT INTO request_tracks "
                    "(id,request_id,ordinal,artist,title,album,album_artist,year,"
                    "duration_seconds,recording_mbid,release_mbid,release_group_mbid,"
                    "source_url,source_extractor,source_id,version_signature,rationale,"
                    "evidence_json,duplicate_status,duplicate_track_id,selected,approved_at,"
                    "rejected_at,metadata_confidence,metadata_provenance_json,created_at,"
                    "updated_at) VALUES "
                    "(:id,:request_id,:ordinal,:artist,:title,:album,:artist,2000,266.0,"
                    ":recording_mbid,:release_mbid,:release_group_mbid,:source_url,"
                    ":source_extractor,:source_id,'studio','legacy fixture','[]','none',NULL,"
                    "1,:created_at,NULL,0.7,:metadata_provenance,:created_at,:created_at)"
                ),
                track_rows,
            )
            job_rows = []
            for track_key, job_key in (
                ("metadata_track", "metadata_job"),
                ("source_track", "source_job"),
                ("incomplete_track", "incomplete_job"),
                ("automatic_track", "automatic_job"),
            ):
                snapshot = {
                    "request_track_id": identifiers[track_key],
                    "artist": "Coldplay",
                    "title": "Yellow",
                    "album": "Legacy Album",
                    "version_signature": "studio",
                }
                if track_key == "metadata_track":
                    snapshot["recording_mbid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                if track_key == "automatic_track":
                    snapshot.update(
                        recording_mbid=automatic_recording_mbid,
                        release_mbid=automatic_release_mbid,
                        release_group_mbid=automatic_release_group_mbid,
                    )
                if track_key == "source_track":
                    snapshot.update(
                        source_url="https://www.youtube.com/watch?v=legacy123",
                        source_extractor="youtube",
                        source_id="legacy123",
                    )
                job_rows.append(
                    {
                        "id": identifiers[job_key],
                        "request_track_id": identifiers[track_key],
                        "snapshot": json.dumps(snapshot, separators=(",", ":")),
                        "dedup_key": f"legacy:{job_key}",
                        "created_at": created_at,
                    }
                )
            connection.execute(
                text(
                    "INSERT INTO download_jobs "
                    "(id,request_track_id,approved_snapshot_json,dedup_key,status,stage,"
                    "progress,priority,source_extractor,source_id,available_at,retry_count,"
                    "lease_token,lease_expires_at,cancel_requested_at,warnings_json,error_code,"
                    "error_message,final_track_id,final_relative_path,final_sha256,completed_at,"
                    "created_at,updated_at) VALUES "
                    "(:id,:request_track_id,:snapshot,:dedup_key,'queued','queued',0.0,0,NULL,"
                    "NULL,:created_at,0,NULL,NULL,NULL,'[]',NULL,NULL,NULL,NULL,NULL,NULL,"
                    ":created_at,:created_at)"
                ),
                job_rows,
            )
            option_rows = [
                {
                    "id": identifiers["metadata_option"],
                    "job_id": identifiers["metadata_job"],
                    "kind": "metadata",
                    "rank": 1,
                    "payload": json.dumps(
                        {
                            "artist": "Coldplay",
                            "title": "Yellow",
                            "album": "Parachutes",
                            "recording_mbid": recording_mbid,
                            "release_mbid": release_mbid,
                            "release_group_mbid": release_group_mbid,
                        },
                        separators=(",", ":"),
                    ),
                    "score": 0.92,
                    "selected_at": created_at,
                },
                {
                    "id": identifiers["metadata_alternative"],
                    "job_id": identifiers["metadata_job"],
                    "kind": "metadata",
                    "rank": 2,
                    "payload": json.dumps(
                        {"artist": "Coldplay", "title": "Yellow", "album": "Live 2003"},
                        separators=(",", ":"),
                    ),
                    "score": 0.8,
                    "selected_at": None,
                },
                {
                    "id": identifiers["source_option"],
                    "job_id": identifiers["source_job"],
                    "kind": "source",
                    "rank": 1,
                    "payload": json.dumps(
                        {
                            "source_id": "legacy123",
                            "source_extractor": "youtube",
                            "url": "https://www.youtube.com/watch?v=legacy123",
                            "title": "Coldplay - Yellow (Official Video)",
                            "channel": "Coldplay",
                            "duration_seconds": 266.0,
                        },
                        separators=(",", ":"),
                    ),
                    "score": 0.91,
                    "selected_at": created_at,
                },
                {
                    "id": identifiers["incomplete_option"],
                    "job_id": identifiers["incomplete_job"],
                    "kind": "source",
                    "rank": 1,
                    "payload": json.dumps(
                        {
                            "source_id": "missing-url",
                            "source_extractor": "youtube",
                            "title": "Coldplay - Yellow",
                        },
                        separators=(",", ":"),
                    ),
                    "score": 0.9,
                    "selected_at": created_at,
                },
                {
                    "id": identifiers["unsafe_source_option"],
                    "job_id": identifiers["incomplete_job"],
                    "kind": "source",
                    "rank": 2,
                    "payload": json.dumps(
                        {
                            "source_id": "attacker-id",
                            "source_extractor": "youtube",
                            "url": "https://notyoutube.com/watch?v=attacker-id",
                            "title": "Coldplay - Yellow",
                        },
                        separators=(",", ":"),
                    ),
                    "score": 0.89,
                    "selected_at": None,
                },
            ]
            connection.execute(
                text(
                    "INSERT INTO job_review_options "
                    "(id,job_id,kind,rank,provider_payload_json,score,selected_at) VALUES "
                    "(:id,:job_id,:kind,:rank,:payload,:score,:selected_at)"
                ),
                option_rows,
            )
    finally:
        engine.dispose()

    return identifiers


def _normalized_sql(value: object) -> str:
    return " ".join(str(value).split())


def _index_signature(index: Index) -> tuple[str | None, tuple[str, ...], bool, str | None]:
    predicate = index.dialect_options["sqlite"].get("where")
    return (
        index.name,
        tuple(column.name for column in index.columns),
        bool(index.unique),
        _normalized_sql(predicate) if predicate is not None else None,
    )


def _reflected_index_signature(
    index: Mapping[str, object],
) -> tuple[str | None, tuple[str, ...], bool, str | None]:
    options = index.get("dialect_options")
    dialect_options = options if isinstance(options, Mapping) else {}
    predicate = dialect_options.get("sqlite_where")
    column_names = index.get("column_names")
    assert isinstance(column_names, list)
    return (
        str(index["name"]) if index.get("name") is not None else None,
        tuple(str(column) for column in column_names),
        bool(index.get("unique")),
        _normalized_sql(predicate) if predicate is not None else None,
    )


def _sqlite_check_constraints(sql: str) -> set[tuple[str, str]]:
    """Read balanced CHECK expressions, including native ADD COLUMN checks.

    SQLAlchemy's SQLite regex assumes one table-level CHECK per line and can
    accidentally absorb later inline REFERENCES expressions. Compare the actual
    stored DDL without dropping any constraint from the metadata assertion.
    """
    checks: set[tuple[str, str]] = set()
    for match in re.finditer(r'CONSTRAINT\s+"?([a-zA-Z0-9_]+)"?\s+CHECK\s*\(', sql):
        start = match.end()
        depth = 1
        quoted = False
        index = start
        while depth:
            character = sql[index]
            if character == "'":
                if quoted and index + 1 < len(sql) and sql[index + 1] == "'":
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted:
                depth += int(character == "(") - int(character == ")")
            index += 1
        checks.add((match.group(1), _normalized_sql(sql[start : index - 1])))
    return checks


def _assert_schema_matches_metadata(database_path: Path) -> None:
    settings = Settings(environment="test", database_path=database_path)
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []

        inspector = inspect(engine)
        for table in Base.metadata.sorted_tables:
            expected_checks = {
                (constraint.name, _normalized_sql(constraint.sqltext))
                for constraint in table.constraints
                if isinstance(constraint, CheckConstraint)
            }
            with engine.connect() as connection:
                table_sql = connection.scalar(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
                    {"name": table.name},
                )
            reflected_checks = _sqlite_check_constraints(table_sql)
            assert reflected_checks == expected_checks, table.name

            expected_indexes = {_index_signature(index) for index in table.indexes}
            reflected_indexes = {
                _reflected_index_signature(index) for index in inspector.get_indexes(table.name)
            }
            assert reflected_indexes == expected_indexes, table.name
    finally:
        engine.dispose()


def test_fresh_migration_persists_revision_and_is_idempotent(settings: Settings) -> None:
    migrate(settings)
    migrate(settings)

    engine = create_database_engine(settings)
    try:
        assert current_revision(engine) == "0004"
    finally:
        engine.dispose()
    validate(settings)
    _assert_schema_matches_metadata(settings.database_path)

    with TestClient(create_app(settings), client=("127.0.0.1", 5050)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/setup").status_code == 200


def test_0002_does_not_promote_incomplete_legacy_metadata_to_canonical_authority() -> None:
    namespace = runpy.run_path(str(MIGRATION_DIRECTORY / "0002_source_decisions_and_hardening.py"))
    selected_legacy_metadata = namespace["_selected_legacy_metadata"]
    selected_at = datetime(2026, 1, 2, tzinfo=UTC)

    assert (
        selected_legacy_metadata(
            [
                {
                    "selected_at": selected_at,
                    "kind": "metadata",
                    "provider_payload_json": json.dumps(
                        {"recording_mbid": "11111111-1111-4111-8111-111111111111"}
                    ),
                    "request_track_id": "track-without-identity-fields",
                }
            ]
        )
        == {}
    )


def test_0002_conservatively_backfills_legacy_selected_reviews(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-reviews.db"
    _upgrade(database_path, "0001")
    identifiers = _seed_legacy_review_state(database_path)

    _upgrade(database_path, "0002")

    engine = create_database_engine(Settings(environment="test", database_path=database_path))
    try:
        with engine.connect() as connection:
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
            decisions = {
                (str(row.job_id), str(row.category)): row
                for row in connection.execute(
                    text(
                        "SELECT job_id,category,state,selected_payload_json,decided_by,"
                        "candidate_set_fingerprint,revision,decided_at "
                        "FROM job_decisions ORDER BY job_id,category"
                    )
                )
            }
            assert len(decisions) == 3

            metadata = decisions[(identifiers["metadata_job"], "canonical_metadata")]
            assert metadata.state == "selected"
            assert metadata.decided_by == "migration"
            assert metadata.decided_at is not None
            assert len(metadata.candidate_set_fingerprint) == 64
            metadata_payload = json.loads(metadata.selected_payload_json)
            assert metadata_payload["recording_mbid"] == identifiers["recording_mbid"]
            assert metadata_payload["release_mbid"] == identifiers["release_mbid"]

            source = decisions[(identifiers["source_job"], "acquisition_source")]
            assert source.state == "selected"
            assert source.decided_by == "migration"
            source_payload = json.loads(source.selected_payload_json)
            assert source_payload["legacy_requires_revalidation"] is True
            assert source_payload["source_candidate_id"]
            assert source_payload["evidence_reference_id"]

            source_candidate = connection.execute(
                text(
                    "SELECT acquisition_url,policy_status,probe_status,evidence_id "
                    "FROM source_candidates WHERE id=:id"
                ),
                {"id": source_payload["source_candidate_id"]},
            ).one()
            assert source_candidate.acquisition_url is None
            assert source_candidate.policy_status == "pending"
            assert source_candidate.probe_status == "pending"
            assert source_candidate.evidence_id == source_payload["evidence_reference_id"]
            evidence = connection.execute(
                text(
                    "SELECT canonical_url,status,sanitized_metadata_json "
                    "FROM evidence_references WHERE id=:id"
                ),
                {"id": source_payload["evidence_reference_id"]},
            ).one()
            assert evidence.canonical_url == "https://www.youtube.com/watch?v=legacy123"
            assert evidence.status == "available"
            assert json.loads(evidence.sanitized_metadata_json)["requires_revalidation"] is True

            incomplete = decisions[(identifiers["incomplete_job"], "acquisition_source")]
            assert incomplete.state == "pending"
            assert incomplete.decided_by is None
            assert incomplete.selected_payload_json is None
            assert incomplete.decided_at is None

            review_options = {
                str(row.id): row
                for row in connection.execute(
                    text(
                        "SELECT id,decision_id,kind,option_key,fingerprint,revision,"
                        "selected_at FROM job_review_options ORDER BY id"
                    )
                )
            }
            assert all(option.decision_id for option in review_options.values())
            assert all(len(option.fingerprint) == 64 for option in review_options.values())
            assert review_options[identifiers["metadata_option"]].selected_at is not None
            assert review_options[identifiers["source_option"]].selected_at is not None
            assert review_options[identifiers["incomplete_option"]].selected_at is None
            assert review_options[identifiers["source_option"]].kind == "acquisition_source"
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM source_candidates WHERE request_track_id=:track_id"),
                    {"track_id": identifiers["incomplete_track"]},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM evidence_references WHERE request_track_id=:track_id"
                    ),
                    {"track_id": identifiers["incomplete_track"]},
                )
                == 0
            )

            canonical_track = connection.execute(
                text(
                    "SELECT recording_mbid,release_mbid,release_group_mbid,"
                    "suggested_recording_mbid,canonical_identity_verified,"
                    "metadata_provenance_json FROM request_tracks WHERE id=:id"
                ),
                {"id": identifiers["metadata_track"]},
            ).one()
            assert canonical_track.recording_mbid == identifiers["recording_mbid"]
            assert canonical_track.release_mbid == identifiers["release_mbid"]
            assert canonical_track.release_group_mbid == identifiers["release_group_mbid"]
            assert canonical_track.suggested_recording_mbid == (
                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            )
            assert canonical_track.canonical_identity_verified
            assert json.loads(canonical_track.metadata_provenance_json)["decided_by"] == (
                "migration"
            )

            automatic_track = connection.execute(
                text(
                    "SELECT recording_mbid,release_mbid,release_group_mbid,"
                    "canonical_identity_verified,metadata_provenance_json "
                    "FROM request_tracks WHERE id=:id"
                ),
                {"id": identifiers["automatic_track"]},
            ).one()
            assert automatic_track.recording_mbid == identifiers["automatic_recording_mbid"]
            assert automatic_track.release_mbid == identifiers["automatic_release_mbid"]
            assert automatic_track.release_group_mbid == identifiers["automatic_release_group_mbid"]
            assert automatic_track.canonical_identity_verified

            automatic_snapshot = json.loads(
                connection.scalar(
                    text("SELECT approved_snapshot_json FROM download_jobs WHERE id=:id"),
                    {"id": identifiers["automatic_job"]},
                )
            )
            assert automatic_snapshot["recording_mbid"] == identifiers["automatic_recording_mbid"]
            assert automatic_snapshot["release_mbid"] == identifiers["automatic_release_mbid"]
            assert (
                automatic_snapshot["release_group_mbid"]
                == identifiers["automatic_release_group_mbid"]
            )
            assert automatic_snapshot["canonical_identity_verified"] is True
            assert automatic_snapshot["metadata_provenance"]["automatic_association"] is True
            assert (
                automatic_snapshot["metadata_provenance"]["source"]
                == "musicbrainz_search_recordings"
            )

            migrated_source_track = connection.execute(
                text(
                    "SELECT source_url,source_extractor,source_id FROM request_tracks WHERE id=:id"
                ),
                {"id": identifiers["source_track"]},
            ).one()
            assert tuple(migrated_source_track) == (None, None, None)
            source_job = connection.execute(
                text(
                    "SELECT active_source_candidate_id,decision_revision,review_round_count,"
                    "approved_snapshot_json FROM download_jobs WHERE id=:id"
                ),
                {"id": identifiers["source_job"]},
            ).one()
            assert source_job.active_source_candidate_id is None
            assert source_job.decision_revision == 1
            assert source_job.review_round_count == 0
            source_snapshot = json.loads(source_job.approved_snapshot_json)
            assert "source_url" not in source_snapshot
            assert "source_extractor" not in source_snapshot
            assert "source_id" not in source_snapshot
            assert (
                source_snapshot["legacy_source_candidate_id"]
                == source_payload["source_candidate_id"]
            )
            incomplete_rounds = connection.scalar(
                text("SELECT review_round_count FROM download_jobs WHERE id=:id"),
                {"id": identifiers["incomplete_job"]},
            )
            assert incomplete_rounds == 1
    finally:
        engine.dispose()

    _downgrade(database_path, "0001")
    downgraded_engine = create_database_engine(
        Settings(environment="test", database_path=database_path)
    )
    try:
        assert current_revision(downgraded_engine) == "0001"
        with downgraded_engine.connect() as connection:
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        downgraded_engine.dispose()


def test_installed_wheel_fresh_migration_matches_current_metadata(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheel"
    installed_directory = tmp_path / "installed"
    runtime_directory = tmp_path / "runtime"
    wheel_directory.mkdir()
    runtime_directory.mkdir()
    subprocess.run(  # noqa: S603 - fixed interpreter builds the repository test fixture
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_directory),
            str(REPO_ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(wheel_directory.glob("music_agent-*.whl"))
    assert len(wheels) == 1
    subprocess.run(  # noqa: S603 - fixed interpreter installs the repository test fixture
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--target",
            str(installed_directory),
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    database_path = runtime_directory / "music-agent.db"
    paths = {
        "MUSIC_AGENT_DATABASE_PATH": database_path,
        "MUSIC_AGENT_ARTWORK_PATH": runtime_directory / "artwork",
        "MUSIC_AGENT_DOWNLOADS_PATH": runtime_directory / "downloads",
        "MUSIC_AGENT_MUSIC_PATH": runtime_directory / "music",
        "MUSIC_AGENT_BACKUP_PATH": runtime_directory / "backups",
    }
    for path in paths.values():
        if path != database_path:
            path.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("MUSIC_AGENT_")
    }
    environment.update(
        {
            "MUSIC_AGENT_ENVIRONMENT": "test",
            "PYTHONPATH": str(installed_directory),
            **{key: str(value) for key, value in paths.items()},
        }
    )
    result = subprocess.run(  # noqa: S603 - installed wheel is the test fixture
        [
            sys.executable,
            "-c",
            "import app; from app.cli import main; "
            "print(app.__file__); main(['migrate']); main(['migrate'])",
        ],
        cwd=runtime_directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert str(installed_directory) in result.stdout
    _assert_schema_matches_metadata(database_path)


def test_migration_history_does_not_import_live_models() -> None:
    migrations = sorted(MIGRATION_DIRECTORY.glob("*.py"))
    assert migrations
    for migration in migrations:
        source = migration.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(migration))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "app.db.models" not in imported_modules, migration.name
        assert "Base.metadata" not in source, migration.name


def test_populated_0002_upgrade_preserves_admin_session_history_and_media(tmp_path: Path) -> None:
    database_path = tmp_path / "production.db"
    _upgrade(database_path, "0001")
    ids = _seed_legacy_review_state(database_path)
    _upgrade(database_path, "0002")
    settings = Settings(environment="test", database_path=database_path)
    engine = create_database_engine(settings)
    metadata = sa.MetaData()
    token = "legacy-session-opaque-value-retained-by-migration"  # noqa: S105 - fixture
    now = datetime.now(UTC)
    media = tmp_path / "music"
    media.mkdir()
    files = [media / name for name in ("Studio.MP3", "Studio.M4A", "Studio.FLAC", "Missing.opus")]
    for path in files[:-1]:
        path.write_bytes(b"synthetic existing file, must never be touched by migration")
    before_media = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files[:-1]}
    with engine.begin() as connection:
        metadata.reflect(bind=connection)
        tables = metadata.tables
        users = tables["users"]
        first_user = dict(connection.execute(sa.select(users)).mappings().one())
        # The literal username 'admin' belongs to the later account. Role
        # migration must choose age/ID, never a magic username.
        connection.execute(
            users.update().values(username="original-owner", username_normalized="original-owner")
        )
        later_user = {
            **first_user,
            "id": "later-user",
            "username": "admin",
            "username_normalized": "admin",
            "created_at": now,
            "is_active": False,
        }
        connection.execute(users.insert().values(later_user))
        conversation = dict(connection.execute(sa.select(tables["conversations"])).mappings().one())
        connection.execute(
            tables["conversations"]
            .insert()
            .values({**conversation, "id": "later-conversation", "user_id": "later-user"})
        )
        request = dict(connection.execute(sa.select(tables["requests"])).mappings().one())
        connection.execute(
            tables["requests"]
            .insert()
            .values(
                {
                    **request,
                    "id": "later-request",
                    "user_id": "later-user",
                    "conversation_id": "later-conversation",
                }
            )
        )
        item = dict(
            connection.execute(sa.select(tables["request_tracks"]).limit(1)).mappings().one()
        )
        connection.execute(
            tables["request_tracks"]
            .insert()
            .values({**item, "id": "later-item", "request_id": "later-request"})
        )
        job = dict(connection.execute(sa.select(tables["download_jobs"]).limit(1)).mappings().one())
        connection.execute(
            tables["download_jobs"]
            .insert()
            .values(
                {
                    **job,
                    "id": "later-job",
                    "request_track_id": "later-item",
                    "dedup_key": "later-job",
                }
            )
        )
        for key, state in (
            ("metadata_job", "completed"),
            ("source_job", "failed"),
            ("incomplete_job", "active"),
            ("automatic_job", "needs_review"),
        ):
            connection.execute(
                tables["download_jobs"]
                .update()
                .where(tables["download_jobs"].c.id == ids[key])
                .values(status=state)
            )
        connection.execute(
            tables["sessions"]
            .insert()
            .values(
                id="old-session",
                user_id=ids["user"],
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                csrf_hash="a" * 64,
                created_at=now,
                last_activity_at=now,
                idle_expires_at=datetime(2099, 1, 1),
                absolute_expires_at=datetime(2099, 1, 1),
            )
        )
        connection.execute(
            tables["albums"]
            .insert()
            .values(
                id="old-album",
                identity_key="old-album",
                artist="Artist",
                artist_normalized="artist",
                title="Album",
                title_normalized="album",
                created_at=now,
                updated_at=now,
            )
        )
        for index, path in enumerate(files):
            connection.execute(
                tables["tracks"]
                .insert()
                .values(
                    id=f"old-track-{index}",
                    album_id="old-album",
                    artist="Artist",
                    artist_normalized="artist",
                    title=f"Track {index}",
                    title_normalized=f"track {index}",
                    version_signature="studio",
                    filepath=path.name,
                    is_present=path.exists(),
                    codec="old-codec",
                    file_mtime_ns=123,
                    file_size=17,
                    provenance_json='{"preserve":"original"}',
                    scan_generation=7,
                    created_at=now,
                    updated_at=now,
                )
            )
        for index, state in enumerate(("completed", "running", "running")):
            connection.execute(
                tables["scan_runs"]
                .insert()
                .values(
                    id=f"old-scan-{index}",
                    kind="full",
                    generation=index + 1,
                    status=state,
                    scanned_files=15_000,
                    changed_files=2,
                    missing_files=3,
                    error_count=1,
                    started_at=now,
                )
            )
        for identifier, request_id, total in (
            ("direct-call", ids["request"], 15),
            ("decision-call", None, 20),
            ("ambiguous-call", None, 10),
            ("unknown-zero-call", None, 0),
        ):
            connection.execute(
                tables["openai_calls"]
                .insert()
                .values(
                    id=identifier,
                    request_id=request_id,
                    model="gpt-5.6-luna",
                    prompt_version="old-prompt",
                    prompt_hash="b" * 64,
                    input_tokens=total,
                    cached_input_tokens=0,
                    cache_write_tokens=0,
                    output_tokens=0,
                    reasoning_tokens=0,
                    total_tokens=total,
                    web_search_count=0,
                    latency_ms=10,
                    status="completed",
                    pricing_snapshot_json="{}",
                    estimated_cost_microusd=total,
                    created_at=now,
                )
            )
        decisions = tables["job_decisions"]
        connection.execute(
            decisions.update()
            .where(decisions.c.job_id == ids["source_job"])
            .values(openai_call_id="decision-call")
        )
        connection.execute(
            decisions.update()
            .where(decisions.c.job_id == ids["metadata_job"])
            .values(openai_call_id="ambiguous-call")
        )
        selected = dict(
            connection.execute(
                sa.select(decisions).where(decisions.c.job_id == ids["metadata_job"])
            )
            .mappings()
            .one()
        )
        connection.execute(
            decisions.insert().values({**selected, "id": "later-decision", "job_id": "later-job"})
        )
        for index, (entity_type, entity_id) in enumerate(
            (
                ("request", ids["request"]),
                ("job", ids["metadata_job"]),
                ("library", None),
                ("request", "missing-request"),
                ("request", "later-request"),
            )
        ):
            connection.execute(
                tables["events"]
                .insert()
                .values(
                    id=100 + index,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_type="legacy.event",
                    message="preserve event",
                    details_json=json.dumps({"user_id": ids["user"]}),
                    created_at=now,
                )
            )
        before = {
            name: [
                dict(row)
                for row in connection.execute(
                    sa.select(table).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for name, table in tables.items()
            if name != "alembic_version"
        }
    engine.dispose()

    _upgrade(database_path, "head")
    _upgrade(database_path, "head")
    _assert_schema_matches_metadata(database_path)
    engine = create_database_engine(settings)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "delete"
            for name, original_rows in before.items():
                table = tables[name]
                rows = [
                    dict(row)
                    for row in connection.execute(
                        sa.select(table).order_by(*table.primary_key.columns)
                    ).mappings()
                ]
                if name == "scan_runs":
                    original_rows = [
                        {
                            **row,
                            "status": "failed",
                            "error_message": "interrupted by schema upgrade",
                        }
                        if row["status"] == "running"
                        else row
                        for row in original_rows
                    ]
                assert rows == original_rows, name
            roles = dict(connection.execute(sa.text("SELECT id,role FROM users")).all())
            assert roles == {ids["user"]: "admin", "later-user": "user"}
            assert connection.execute(
                sa.text("SELECT is_active,disabled_at FROM users WHERE id='later-user'")
            ).one() == (0, None)
            assert (
                connection.scalar(
                    sa.text("SELECT count(*) FROM download_jobs WHERE dismissed_at IS NOT NULL")
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM request_tracks WHERE canonical_identity_verified=1"
                    )
                )
                >= 2
            )
            owners = dict(
                connection.execute(sa.text("SELECT id,owner_user_id FROM openai_calls")).all()
            )
            assert owners == {
                "direct-call": ids["user"],
                "decision-call": ids["user"],
                "ambiguous-call": None,
                "unknown-zero-call": None,
            }
            usage = dict(
                connection.execute(sa.text("SELECT id,usage_reported FROM openai_calls")).all()
            )
            assert usage["unknown-zero-call"] is None
            assert all(
                usage[key] == 1 for key in ("direct-call", "decision-call", "ambiguous-call")
            )
            audiences = {
                row[0]: (row[1], row[2])
                for row in connection.execute(sa.text("SELECT id,audience,user_id FROM events"))
            }
            assert audiences == {
                100: ("user", ids["user"]),
                101: ("user", ids["user"]),
                102: ("admin", None),
                103: ("admin", None),
                104: ("user", "later-user"),
            }
            assert connection.execute(
                sa.text("SELECT file_extension,parser_version FROM tracks ORDER BY id")
            ).all() == [(".mp3", 0), (".m4a", 0), (".flac", 0), (".opus", 0)]
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM requests WHERE orchestration_attempt_id IS NOT NULL "
                        "OR termination_reason IS NOT NULL OR configured_agent_seconds IS NOT NULL"
                    )
                )
                == 0
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM openai_calls "
                        "WHERE configured_agent_seconds IS NOT NULL"
                    )
                )
                == 0
            )
        factory = sa.orm.sessionmaker(engine, expire_on_commit=False)
        authenticated = AuthRepository(engine, factory, settings).resolve_session(token)
        assert authenticated is not None
        assert authenticated.user_id == ids["user"] and authenticated.role == "admin"
        assert authenticated.username == "original-owner"
    finally:
        engine.dispose()
    assert before_media == {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files[:-1]
    }
    assert not files[-1].exists()


def test_new_native_constraints_are_enforced_and_unsafe_downgrade_is_refused(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "constraints.db"
    _upgrade(database_path, "0001")
    ids = _seed_legacy_review_state(database_path)
    _upgrade(database_path, "head")
    engine = create_database_engine(Settings(environment="test", database_path=database_path))
    try:
        for statement in (
            "UPDATE users SET role='superuser'",
            "UPDATE users SET created_by_user_id='missing-user'",
            "INSERT INTO events(entity_type,event_type,message,details_json,created_at,audience) "
            "VALUES('request','invalid','invalid','{}','2026-01-01','user')",
            "INSERT INTO events(entity_type,event_type,message,details_json,"
            "created_at,audience,user_id) "
            "VALUES('request','invalid','invalid','{}','2026-01-01','admin',:owner)",
        ):
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
                connection.execute(sa.text(statement), {"owner": ids["user"]})
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO scan_runs(id,kind,generation,status,scanned_files,changed_files,"
                    "missing_files,error_count,started_at) "
                    "VALUES('one','full',1,'running',0,0,0,0,'2026-01-01')"
                )
            )
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO scan_runs(id,kind,generation,status,scanned_files,changed_files,"
                    "missing_files,error_count,started_at) "
                    "VALUES('two','full',2,'running',0,0,0,0,'2026-01-01')"
                )
            )
        with pytest.raises(RuntimeError, match="paired pre-update"):
            _downgrade(database_path, "0002")
        assert current_revision(engine) == "0004"
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
