from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.cli import config_check, main
from app.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["127.0.0.0/8","::1/128"]', ["127.0.0.0/8", "::1/128"]),
        ("127.0.0.0/8, ::1/128", ["127.0.0.0/8", "::1/128"]),
        ("", []),
    ],
)
def test_list_settings_accept_json_csv_and_empty(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("MUSIC_AGENT_TRUSTED_PROXY_CIDRS", raw)
    assert Settings().trusted_proxy_cidrs == expected


@pytest.mark.parametrize(
    "raw",
    ["127.0.0.0/8,", "127.0.0.0/8,,::1/128", "[invalid", '["ok", 3]', "{}"],
)
def test_list_settings_reject_malformed_members(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("MUSIC_AGENT_TRUSTED_PROXY_CIDRS", raw)
    with pytest.raises((SettingsError, ValidationError)):
        Settings()


def test_list_settings_normalize_and_deduplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "MUSIC_AGENT_ALLOWED_CLIENT_CIDRS",
        '["192.168.1.7/24","192.168.1.0/24","::1"]',
    )
    monkeypatch.setenv("MUSIC_AGENT_ENABLED_MEDIA_PROVIDERS", '["YouTube","soundcloud","youtube"]')
    monkeypatch.setenv("MUSIC_AGENT_MEDIA_PROVIDER_PREFERENCE", '["youtube","soundcloud"]')
    settings = Settings()
    assert settings.allowed_client_cidrs == ["192.168.1.0/24", "::1/128"]
    assert settings.enabled_media_providers == ["youtube", "soundcloud"]


def test_origins_hosts_and_source_policy_are_validated() -> None:
    settings = Settings(
        public_base_url="HTTPS://Müsic.Example:443/",
        allowed_browser_origins=["http://music-server:80", "https://MÜSIC.example"],
        trusted_hosts=["localhost"],
    )
    assert settings.public_base_url == "https://xn--msic-0ra.example"
    assert settings.allowed_browser_origins == [
        "http://music-server",
        "https://xn--msic-0ra.example",
    ]
    assert settings.effective_trusted_hosts == ["localhost", "xn--msic-0ra.example"]

    with pytest.raises(ValidationError, match="public_supported"):
        Settings(media_source_policy="public_supported")
    with pytest.raises(ValidationError, match="generic extraction"):
        Settings(environment="production", service_role="worker", allow_generic_extractor=True)
    with pytest.raises(ValidationError, match="unrestricted host wildcards"):
        Settings(trusted_hosts=["*"])
    with pytest.raises(ValidationError, match="IP address wildcards"):
        Settings(trusted_hosts=["*.127.0.0.1"])
    with pytest.raises(ValidationError, match="empty port"):
        Settings(public_base_url="https://music.example:")
    with pytest.raises(ValidationError, match="invalid host"):
        Settings(allowed_browser_origins=["https://-invalid.example"])
    with pytest.raises(ValidationError, match="both allowed and blocked"):
        Settings(allowed_media_extractors=["generic"])


def test_ai_thresholds_are_independent_confidence_dimensions() -> None:
    settings = Settings(
        ai_match_auto_accept_threshold=0.90,
        ai_match_min_local_score=0.95,
    )
    assert settings.ai_match_min_local_score == 0.95


def test_review_javascript_targets_the_dedicated_correction_panel() -> None:
    javascript = (REPO_ROOT / "app/static/app.js").read_text(encoding="utf-8")
    template = (REPO_ROOT / "app/templates/downloads.html").read_text(encoding="utf-8")

    assert 'querySelector("details.review-correction")' in javascript
    assert 'querySelector("details:last-of-type")' not in javascript
    assert '<details class="review-correction">' in template


def test_config_check_does_not_open_database_or_create_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(
        database_path=tmp_path / "missing" / "music-agent.db",
        artwork_path=tmp_path / "missing" / "artwork",
        downloads_path=tmp_path / "missing" / "downloads",
        music_path=tmp_path / "missing" / "music",
        backup_path=tmp_path / "missing" / "backups",
    )

    config_check(settings)

    assert not (tmp_path / "missing").exists()
    assert "strict OpenAI schemas are valid" in capsys.readouterr().out


def test_config_check_validates_web_and_worker_roles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_check(Settings(), all_roles=True)

    output = capsys.readouterr().out
    assert "web" in output
    assert "worker" in output


def test_credentialless_config_check_validates_production_roles_without_secret_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        environment="production",
        service_role="worker",
        database_path=Path("/var/lib/music-agent/music-agent.db"),
        artwork_path=Path("/var/lib/music-agent/artwork"),
        downloads_path=Path("/srv/music-downloads"),
        music_path=Path("/srv/music"),
        backup_path=Path("/var/lib/music-agent/backups"),
    )

    config_check(settings, all_roles=True, without_runtime_credentials=True)

    output = capsys.readouterr().out
    assert "web" in output
    assert "worker" in output


def test_credentialless_config_check_requires_all_roles() -> None:
    with pytest.raises(ValueError, match="requires --all-roles"):
        config_check(Settings(), without_runtime_credentials=True)


def test_config_check_rejects_unreviewed_extractor_and_host() -> None:
    with pytest.raises(RuntimeError, match="extractor is not reviewed"):
        config_check(Settings(allowed_media_extractors=["unreviewed"]))

    with pytest.raises(RuntimeError, match="host is not reviewed"):
        config_check(Settings(allowed_media_hosts=["media.invalid"]))


def test_config_check_rejects_source_attempts_above_candidate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_AGENT_MAX_AUTOMATIC_SOURCE_ATTEMPTS", "10")
    monkeypatch.setenv("MUSIC_AGENT_MAX_SOURCE_CANDIDATES", "5")

    with pytest.raises(ValidationError, match="automatic source attempts"):
        main(["config-check"])


def test_config_check_rejects_blocked_curated_extractor_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MUSIC_AGENT_BLOCKED_MEDIA_EXTRACTORS", '["generic","youtube:tab"]')

    with pytest.raises(ValidationError, match="curated extractor aliases"):
        main(["config-check"])


def test_production_rejects_paths_outside_native_managed_layout() -> None:
    with pytest.raises(ValidationError, match="production database_path must use managed path"):
        Settings(
            environment="production",
            service_role="worker",
            database_path=Path("/var/lib/music-agent/custom.db"),
        )
