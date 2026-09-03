from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.cli import config_check
from app.config import Settings


@pytest.mark.parametrize(
    ("legacy", "canonical", "expected", "source"),
    [
        (None, None, 10, "default"),
        ("20", None, 20, "legacy"),
        ("50", None, 50, "legacy"),
        (None, "50", 50, "canonical"),
        ("50", "50", 50, "both_agree"),
    ],
)
def test_environment_model_round_aliases(
    monkeypatch: pytest.MonkeyPatch,
    legacy: str | None,
    canonical: str | None,
    expected: int,
    source: str,
) -> None:
    for name, value in (
        ("MUSIC_AGENT_MAX_AGENT_STEPS", legacy),
        ("MUSIC_AGENT_MAX_MODEL_ROUNDS", canonical),
    ):
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    settings = Settings()
    assert settings.max_model_rounds == settings.max_agent_steps == expected
    assert settings.model_rounds_configuration_source == source
    assert settings.openai_max_tool_calls == 10


@pytest.mark.parametrize("name", ["MAX_AGENT_STEPS", "MAX_MODEL_ROUNDS", "OPENAI_MAX_TOOL_CALLS"])
@pytest.mark.parametrize("value", ["0", "51", "invalid"])
def test_invalid_budgets_fail_preflight(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(f"MUSIC_AGENT_{name}", value)
    with pytest.raises(ValidationError):
        Settings()


def test_conflicting_aliases_are_not_silently_prioritized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUSIC_AGENT_MAX_MODEL_ROUNDS", "10")
    monkeypatch.setenv("MUSIC_AGENT_MAX_AGENT_STEPS", "50")
    with pytest.raises(ValidationError, match="MAX_MODEL_ROUNDS and MUSIC_AGENT_MAX_AGENT_STEPS"):
        Settings()


def test_constructor_compatibility_and_independent_tool_budget() -> None:
    settings = Settings(max_agent_steps=50, openai_max_tool_calls=3)
    assert settings.max_model_rounds == settings.max_agent_steps == 50
    assert settings.openai_max_tool_calls == 3
    with pytest.raises(ValidationError, match="must agree"):
        Settings(max_agent_steps=50, max_model_rounds=20)


@pytest.mark.parametrize("role", ["web", "worker"])
def test_production_luna_and_legacy_fifty_validate_without_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("MUSIC_AGENT_OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("MUSIC_AGENT_MAX_AGENT_STEPS", "50")
    credential = tmp_path / "auth_hmac_key"
    credential.write_text("test-only-authentication-key-at-least-32-characters")
    settings = Settings(
        environment="production",
        service_role=role,
        credential_directory=tmp_path,
        database_path="/var/lib/music-agent/music-agent.db",
        artwork_path="/var/lib/music-agent/artwork",
        downloads_path="/srv/music-downloads",
        music_path="/srv/music",
        backup_path="/var/lib/music-agent/backups",
    )
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.max_model_rounds == 50
    assert settings.model_rounds_configuration_source == "legacy"


def test_production_preflight_with_legacy_fifty_is_database_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MUSIC_AGENT_OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("MUSIC_AGENT_MAX_AGENT_STEPS", "50")
    settings = Settings(
        environment="production",
        service_role="worker",
        database_path="/var/lib/music-agent/music-agent.db",
        artwork_path="/var/lib/music-agent/artwork",
        downloads_path="/srv/music-downloads",
        music_path="/srv/music",
        backup_path="/var/lib/music-agent/backups",
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("config-check must not open the database")

    monkeypatch.setattr("app.cli.create_database_engine", forbidden)
    config_check(settings, all_roles=True, without_runtime_credentials=True)
    assert "web, worker" in capsys.readouterr().out
    assert settings.max_model_rounds == 50
