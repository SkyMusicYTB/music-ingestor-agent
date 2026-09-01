from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


CsvList = Annotated[list[str], BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSIC_AGENT_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    service_role: Literal["web", "worker"] = "web"
    app_version: str = "0.1.0"
    bind_host: str = "0.0.0.0"  # noqa: S104 - intentionally LAN/Tailscale reachable
    bind_port: int = Field(default=8787, ge=1, le=65535)
    public_base_url: str | None = None
    https_enabled: bool = False

    database_path: Path = Path("dev-data/music-agent.db")
    artwork_path: Path = Path("dev-data/artwork")
    downloads_path: Path = Path("dev-data/downloads")
    music_path: Path = Path("dev-data/music")
    backup_path: Path = Path("dev-data/backups")
    credential_directory: Path | None = None

    allowed_client_cidrs: CsvList = [
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fd7a:115c:a1e0::/48",
    ]
    trusted_hosts: CsvList = ["localhost", "127.0.0.1", "[::1]", "music-server"]
    trusted_proxy_cidrs: CsvList = []

    session_idle_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    session_absolute_seconds: int = Field(default=2_592_000, ge=3600, le=31_536_000)
    auth_window_seconds: int = Field(default=900, ge=60, le=86_400)
    auth_max_failures: int = Field(default=8, ge=2, le=100)
    auth_block_seconds: int = Field(default=900, ge=60, le=86_400)
    auth_hmac_key: SecretStr = SecretStr("development-only-change-me")

    openai_api_key: SecretStr | None = None
    listenbrainz_token: SecretStr | None = None
    listenbrainz_username: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = "low"
    openai_web_search_enabled: bool = False
    max_agent_steps: int = Field(default=10, ge=1, le=20)
    max_agent_seconds: int = Field(default=120, ge=10, le=600)
    openai_max_output_tokens: int = Field(default=12_000, ge=1_000, le=32_000)
    max_candidates_per_request: int = Field(default=250, ge=10, le=500)
    auto_download_exact_single: bool = True

    price_input_per_million_usd: float | None = Field(default=None, ge=0)
    price_cached_input_per_million_usd: float | None = Field(default=None, ge=0)
    price_cache_write_per_million_usd: float | None = Field(default=None, ge=0)
    price_output_per_million_usd: float | None = Field(default=None, ge=0)
    price_web_search_low_usd: float | None = Field(default=None, ge=0)
    price_web_search_medium_usd: float | None = Field(default=None, ge=0)
    price_web_search_high_usd: float | None = Field(default=None, ge=0)

    musicbrainz_user_agent: str = "MusicAgent/0.1 (configure-contact@example.invalid)"
    apple_metadata_enabled: bool = False
    apple_storefront: str = "GB"
    max_direct_media_seconds: int = Field(default=1800, ge=30, le=14_400)
    allow_lossy_transcode: bool = False
    worker_download_slots: int = Field(default=2, ge=1, le=8)
    lease_seconds: int = Field(default=120, ge=30, le=900)
    min_free_bytes: int = Field(default=2_147_483_648, ge=104_857_600)
    initial_scan_required: bool = True
    log_level: str = "INFO"

    @field_validator("database_path", "artwork_path", "downloads_path", "music_path", "backup_path")
    @classmethod
    def absolute_in_production(cls, value: Path, info: object) -> Path:
        return value.expanduser()

    @field_validator("allowed_client_cidrs", "trusted_proxy_cidrs")
    @classmethod
    def validate_networks(cls, values: list[str]) -> list[str]:
        return [str(ipaddress.ip_network(value, strict=False)) for value in values]

    @model_validator(mode="after")
    def load_credentials(self) -> Settings:
        credential_dir = self.credential_directory
        if credential_dir is None:
            inherited = os.environ.get("CREDENTIALS_DIRECTORY")
            if inherited:
                credential_dir = Path(inherited)
        if self.environment == "production":
            if self.service_role == "web":
                if credential_dir is None:
                    raise ValueError("CREDENTIALS_DIRECTORY is required for the web service")
                credential_hmac = self._read_secret(credential_dir / "auth_hmac_key", None)
                if credential_hmac is None:
                    raise ValueError("auth_hmac_key systemd credential is required")
                self.auth_hmac_key = credential_hmac
                # Production deliberately ignores secret-looking environment variables.
                self.openai_api_key = self._read_secret(
                    credential_dir / "openai_api_key", None, optional=True
                )
                self.listenbrainz_token = self._read_secret(
                    credential_dir / "listenbrainz_token", None, optional=True
                )
            else:
                # The worker has no authentication or provider-secret responsibilities.
                # Clear any inherited values and never require a credential directory.
                self.openai_api_key = None
                self.listenbrainz_token = None
            for path in (
                self.database_path,
                self.artwork_path,
                self.downloads_path,
                self.music_path,
                self.backup_path,
            ):
                if not path.is_absolute():
                    raise ValueError(f"production path must be absolute: {path}")
        elif credential_dir:
            self.auth_hmac_key = (
                self._read_secret(credential_dir / "auth_hmac_key", self.auth_hmac_key)
                or self.auth_hmac_key
            )
            self.openai_api_key = self._read_secret(
                credential_dir / "openai_api_key", self.openai_api_key
            )
            self.listenbrainz_token = self._read_secret(
                credential_dir / "listenbrainz_token", self.listenbrainz_token, optional=True
            )
        return self

    @staticmethod
    def _read_secret(
        path: Path, fallback: SecretStr | None, optional: bool = False
    ) -> SecretStr | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            if optional:
                return fallback
            return fallback
        if not value:
            return fallback
        return SecretStr(value)

    @property
    def sqlite_url(self) -> str:
        return f"sqlite+pysqlite:///{self.database_path}"

    @property
    def allowed_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(value) for value in self.allowed_client_cidrs)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
