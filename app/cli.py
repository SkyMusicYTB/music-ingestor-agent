from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config as AlembicConfig

from app.config import Settings
from app.db.engine import (
    assert_database_pragmas,
    assert_schema_current,
    create_database_engine,
    make_session_factory,
)
from app.main import create_app
from app.openai_schema import compile_openai_schema
from app.repositories.auth import AuthRepository
from app.schemas import MusicProposal
from app.services.library_scan import LibraryScanner


def _alembic(settings: Settings) -> AlembicConfig:
    config = AlembicConfig(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.sqlite_url)
    return config


def migrate(settings: Settings) -> None:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic(settings), "head")
    engine = create_database_engine(settings)
    try:
        assert_schema_current(engine)
        assert_database_pragmas(engine)
    finally:
        engine.dispose()


def validate(settings: Settings) -> None:
    engine = create_database_engine(settings)
    try:
        assert_schema_current(engine)
        assert_database_pragmas(engine)
        for path in (
            settings.database_path.parent,
            settings.artwork_path,
            settings.downloads_path,
            settings.music_path,
            settings.backup_path,
        ):
            if not path.exists():
                raise RuntimeError(f"required path does not exist: {path}")
            if not os.access(path, os.R_OK):
                raise RuntimeError(f"required path is not readable: {path}")
    finally:
        engine.dispose()
    print("configuration, schema, SQLite pragmas, and paths are valid")


def config_check(
    settings: Settings,
    *,
    all_roles: bool = False,
    without_runtime_credentials: bool = False,
) -> None:
    """Validate configuration contracts without opening SQLite or creating paths.

    Credentialless validation is used only by root-run deployment preflight. The
    supplied worker-role settings have already passed every shared validator; role
    copies let the schema/provider contracts retain their all-role coverage without
    making web credentials readable by the still-running shared-UID worker.
    """

    from app.sources.decisions import (
        canonical_match_decision_schema,
        source_match_decision_schema,
    )
    from app.sources.identities import ProviderIdentity
    from app.sources.providers import provider_capability, provider_for_extractor
    from app.tools.library import LibrarySearchArguments, LibrarySummaryArguments
    from app.tools.listenbrainz import (
        ArtistRadioArguments,
        PopularRecordingsArguments,
        UserRecommendationsArguments,
    )
    from app.tools.media_sources import (
        ProbeMediaSourceArguments,
        SearchMediaSourcesArguments,
    )
    from app.tools.musicbrainz import RecordingSearchArguments, ReleaseSearchArguments

    if without_runtime_credentials and not all_roles:
        raise ValueError("credentialless config-check requires --all-roles")
    validated = [settings]
    if all_roles:
        if without_runtime_credentials:
            validated = [
                settings.model_copy(update={"service_role": role}) for role in ("web", "worker")
            ]
        else:
            validated = [Settings(service_role="web"), Settings(service_role="worker")]
    response_and_tool_schemas = (
        MusicProposal,
        LibrarySearchArguments,
        LibrarySummaryArguments,
        PopularRecordingsArguments,
        ArtistRadioArguments,
        UserRecommendationsArguments,
        RecordingSearchArguments,
        ReleaseSearchArguments,
        SearchMediaSourcesArguments,
        ProbeMediaSourceArguments,
    )
    for candidate in validated:
        # Settings construction performs role-specific credential and source-policy
        # validation. Compile registered response schemas here so an incompatible
        # model schema cannot survive deployment preflight.
        for schema_model in response_and_tool_schemas:
            compile_openai_schema(schema_model.model_json_schema())
        canonical_format = canonical_match_decision_schema(
            recording_candidate_ids=("recording-candidate",),
            release_candidate_ids=("release-candidate",),
        )
        source_format = source_match_decision_schema(source_candidate_ids=("source-candidate",))
        compile_openai_schema(canonical_format["schema"])
        compile_openai_schema(source_format["schema"])
        enabled = set(candidate.enabled_media_providers)
        for extractor in candidate.allowed_media_extractors:
            provider = provider_for_extractor(extractor)
            if provider is None or provider.value not in enabled:
                raise RuntimeError(
                    f"allowed extractor is not reviewed for an enabled provider: {extractor}"
                )
        for hostname in candidate.allowed_media_hosts:
            concrete_hostname = hostname.removeprefix("*.")
            if not any(
                provider_capability(provider).accepts_hostname(concrete_hostname)
                for provider in ProviderIdentity
                if provider.value in enabled
            ):
                raise RuntimeError(
                    f"allowed media host is not reviewed for an enabled provider: {hostname}"
                )
        if candidate.environment == "production" and candidate.allow_generic_extractor:
            raise RuntimeError("generic extraction is prohibited in production")
    roles = ", ".join(dict.fromkeys(candidate.service_role for candidate in validated))
    print(f"configuration and strict OpenAI schemas are valid for: {roles}")


def admin_reset(settings: Settings) -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("admin-reset must run from an interactive terminal")
    username = input("Admin username: ").strip()
    password = getpass.getpass("New password (12+ characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("passwords do not match")
    engine = create_database_engine(settings)
    factory = make_session_factory(engine)
    try:
        assert_schema_current(engine)
        AuthRepository(engine, factory, settings).reset_admin(username, password)
    finally:
        engine.dispose()
    print("admin credentials reset and all sessions revoked")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="music-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="upgrade the database schema")
    commands.add_parser("validate", help="validate config, paths, schema and SQLite")
    config = commands.add_parser(
        "config-check", help="validate configuration without opening the database"
    )
    config.add_argument("--all-roles", action="store_true")
    config.add_argument(
        "--without-runtime-credentials", action="store_true", help=argparse.SUPPRESS
    )
    scan = commands.add_parser("scan", help="index the music library")
    scan.add_argument("--full", action="store_true")
    commands.add_parser("admin-reset", help="reset the initial admin from a TTY")
    commands.add_parser("web", help="run the web service")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = Settings()
    if args.command == "migrate":
        migrate(settings)
    elif args.command == "validate":
        validate(settings)
    elif args.command == "config-check":
        config_check(
            settings,
            all_roles=args.all_roles,
            without_runtime_credentials=args.without_runtime_credentials,
        )
    elif args.command == "admin-reset":
        admin_reset(settings)
    elif args.command == "scan":
        engine = create_database_engine(settings)
        factory = make_session_factory(engine)
        try:
            assert_schema_current(engine)
            result = LibraryScanner(factory, settings.music_path).run(full=args.full)
        finally:
            engine.dispose()
        print(
            f"scan {result.status}: {result.scanned_files} files, "
            f"{result.changed_files} changed, {result.error_count} errors"
        )
    elif args.command == "web":
        uvicorn.run(
            create_app(settings),
            host=settings.bind_host,
            port=settings.bind_port,
            proxy_headers=False,
            server_header=False,
        )


if __name__ == "__main__":
    main()
