from __future__ import annotations

import ipaddress
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.sources.identities import ProviderIdentity
from app.sources.providers import provider_capability

_EXTRACTOR_NAME = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,99}$")
_VERSION_NAME = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,79}$")
_KNOWN_MEDIA_PROVIDERS = frozenset({"bandcamp", "soundcloud", "youtube"})
_MANAGED_PRODUCTION_PATHS = {
    "database_path": Path("/var/lib/music-agent/music-agent.db"),
    "artwork_path": Path("/var/lib/music-agent/artwork"),
    "downloads_path": Path("/srv/music-downloads"),
    "music_path": Path("/srv/music"),
    "backup_path": Path("/var/lib/music-agent/backups"),
}


def _parse_string_list(value: object) -> object:
    """Parse compact JSON arrays or strict CSV before Pydantic list validation."""

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON array: {error.msg}") from error
            if not isinstance(decoded, list):
                raise ValueError("JSON value must be an array")
            value = decoded
        else:
            value = raw.split(",")
    if not isinstance(value, (list, tuple)):
        raise ValueError("value must be a JSON array or comma-separated list")

    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("list members must be strings")
        normalized = item.strip()
        if not normalized:
            raise ValueError("list members must not be empty")
        parsed.append(normalized)
    return parsed


StringList = Annotated[list[str], NoDecode, BeforeValidator(_parse_string_list)]


def _deduplicate(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _normalized_dns_name(value: str) -> str:
    try:
        return value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError(f"invalid internationalized hostname: {value}") from error


def _normalize_host_pattern(value: str, *, allow_wildcard: bool) -> str:
    raw = value.strip().lower()
    if len(raw) > 255:
        raise ValueError("hostname is too long")
    wildcard = raw.startswith("*.")
    if raw == "*" or (wildcard and not allow_wildcard):
        raise ValueError("unrestricted host wildcards are not allowed")
    if wildcard:
        raw = raw[2:]
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError(f"invalid hostname: {value}")
    if raw.startswith("[") and raw.endswith("]"):
        try:
            normalized = f"[{ipaddress.IPv6Address(raw[1:-1])}]"
        except ipaddress.AddressValueError as error:
            raise ValueError(f"invalid IPv6 host: {value}") from error
    else:
        try:
            normalized = str(ipaddress.ip_address(raw))
        except ValueError as address_error:
            normalized = _normalized_dns_name(raw)
            labels = normalized.split(".")
            if (
                len(normalized) > 253
                or not normalized
                or any(
                    not label
                    or len(label) > 63
                    or label.startswith("-")
                    or label.endswith("-")
                    or not re.fullmatch(r"[a-z0-9-]+", label)
                    for label in labels
                )
            ):
                raise ValueError(f"invalid hostname: {value}") from address_error
    if wildcard:
        try:
            ipaddress.ip_address(normalized.strip("[]"))
        except ValueError:
            return f"*.{normalized}"
        raise ValueError("IP address wildcards are not allowed")
    return normalized


def _normalize_origin(value: str) -> str:
    raw = value.strip()
    if len(raw) > 300:
        raise ValueError("origin is too long")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("origin must use http or https and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("origin must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("origin has an invalid port") from error
    if port is None and parsed.netloc.endswith(":"):
        raise ValueError("origin has an empty port")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError as address_error:
        normalized_host = _normalized_dns_name(host)
        labels = normalized_host.split(".")
        if len(normalized_host) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not re.fullmatch(r"[a-z0-9-]+", label)
            for label in labels
        ):
            raise ValueError("origin has an invalid host") from address_error
    else:
        normalized_host = f"[{address}]" if address.version == 6 else str(address)
    scheme = parsed.scheme.lower()
    if port is not None and port != (443 if scheme == "https" else 80):
        normalized_host = f"{normalized_host}:{port}"
    return urlunsplit((scheme, normalized_host, "", "", ""))


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
    origin_policy: Literal["private_network", "strict"] = "private_network"

    database_path: Path = Path("dev-data/music-agent.db")
    artwork_path: Path = Path("dev-data/artwork")
    downloads_path: Path = Path("dev-data/downloads")
    music_path: Path = Path("dev-data/music")
    backup_path: Path = Path("dev-data/backups")
    credential_directory: Path | None = None

    allowed_client_cidrs: StringList = Field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "fd7a:115c:a1e0::/48",
        ]
    )
    trusted_hosts: StringList = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "[::1]", "music-server"]
    )
    trusted_proxy_cidrs: StringList = Field(default_factory=list)
    allowed_browser_origins: StringList = Field(default_factory=list)

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
    media_source_policy: Literal["curated", "public_supported"] = "curated"
    enabled_media_providers: StringList = Field(
        default_factory=lambda: ["bandcamp", "soundcloud", "youtube"]
    )
    media_provider_preference: StringList = Field(
        default_factory=lambda: ["bandcamp", "soundcloud", "youtube"]
    )
    allowed_media_extractors: StringList = Field(default_factory=list)
    allowed_media_hosts: StringList = Field(default_factory=list)
    blocked_media_extractors: StringList = Field(default_factory=lambda: ["generic"])
    allow_generic_extractor: bool = False
    review_policy: Literal["exception_only"] = "exception_only"
    ai_match_resolution_enabled: bool = True
    ai_match_auto_accept_threshold: float = Field(default=0.90, ge=0, le=1)
    ai_match_min_local_score: float = Field(default=0.75, ge=0, le=1)
    max_automatic_source_attempts: int = Field(default=3, ge=1, le=10)
    source_auto_select_threshold: float = Field(default=0.88, ge=0, le=1)
    source_ambiguity_margin: float = Field(default=0.08, ge=0, le=1)
    default_version_preference: str = "studio"
    max_source_candidates: int = Field(default=24, ge=1, le=100)
    max_visible_source_options: int = Field(default=5, ge=1, le=20)
    max_direct_playlist_items: int = Field(default=25, ge=1, le=100)
    max_media_bytes: int = Field(default=1_073_741_824, ge=1_048_576, le=107_374_182_400)
    source_probe_negative_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    max_review_rounds_per_category: int = Field(default=3, ge=1, le=20)
    max_review_rounds_per_job: int = Field(default=8, ge=1, le=100)
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
        return _deduplicate([str(ipaddress.ip_network(value, strict=False)) for value in values])

    @field_validator("trusted_hosts")
    @classmethod
    def validate_trusted_hosts(cls, values: list[str]) -> list[str]:
        return _deduplicate(
            [_normalize_host_pattern(value, allow_wildcard=True) for value in values]
        )

    @field_validator("allowed_browser_origins")
    @classmethod
    def validate_browser_origins(cls, values: list[str]) -> list[str]:
        return _deduplicate([_normalize_origin(value) for value in values])

    @field_validator("public_base_url", mode="before")
    @classmethod
    def normalize_empty_public_base_url(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_origin(value)

    @field_validator("enabled_media_providers", "media_provider_preference")
    @classmethod
    def validate_media_providers(cls, values: list[str]) -> list[str]:
        normalized = _deduplicate([value.casefold() for value in values])
        unknown = sorted(set(normalized) - _KNOWN_MEDIA_PROVIDERS)
        if unknown:
            raise ValueError(f"unknown media provider(s): {', '.join(unknown)}")
        return normalized

    @field_validator("allowed_media_extractors", "blocked_media_extractors")
    @classmethod
    def validate_media_extractors(cls, values: list[str]) -> list[str]:
        normalized = _deduplicate([value.casefold() for value in values])
        invalid = [value for value in normalized if not _EXTRACTOR_NAME.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid media extractor name: {invalid[0]}")
        return normalized

    @field_validator("allowed_media_hosts")
    @classmethod
    def validate_media_hosts(cls, values: list[str]) -> list[str]:
        return _deduplicate(
            [_normalize_host_pattern(value, allow_wildcard=True) for value in values]
        )

    @field_validator("default_version_preference")
    @classmethod
    def validate_version_preference(cls, value: str) -> str:
        normalized = " ".join(value.casefold().split())
        if not _VERSION_NAME.fullmatch(normalized):
            raise ValueError("default version preference is invalid")
        return normalized

    @model_validator(mode="after")
    def load_credentials(self) -> Settings:
        if not self.enabled_media_providers:
            raise ValueError("at least one media provider must be enabled")
        unavailable_preferences = [
            value
            for value in self.media_provider_preference
            if value not in self.enabled_media_providers
        ]
        if unavailable_preferences:
            raise ValueError(
                "preferred media providers must also be enabled: "
                + ", ".join(unavailable_preferences)
            )
        if self.max_visible_source_options > self.max_source_candidates:
            raise ValueError("visible source options cannot exceed the candidate limit")
        if self.max_automatic_source_attempts > self.max_source_candidates:
            raise ValueError("automatic source attempts cannot exceed the candidate limit")
        if self.media_source_policy == "public_supported" and (
            not self.allowed_media_hosts or not self.allowed_media_extractors
        ):
            raise ValueError("public_supported source policy requires allowed hosts and extractors")
        extractor_conflicts = sorted(
            set(self.allowed_media_extractors) & set(self.blocked_media_extractors)
        )
        if extractor_conflicts:
            raise ValueError(
                "media extractors cannot be both allowed and blocked: "
                + ", ".join(extractor_conflicts)
            )
        if self.media_source_policy == "curated":
            implied_extractors = {
                alias
                for provider in self.enabled_media_providers
                for alias in provider_capability(ProviderIdentity(provider)).extractor_aliases
            }
            implied_conflicts = sorted(implied_extractors & set(self.blocked_media_extractors))
            if implied_conflicts:
                raise ValueError(
                    "curated extractor aliases cannot be blocked: " + ", ".join(implied_conflicts)
                )
        if "generic" in self.allowed_media_extractors and not self.allow_generic_extractor:
            raise ValueError("generic extractor requires explicit opt-in")
        if self.allow_generic_extractor and self.environment == "production":
            raise ValueError("generic extraction is prohibited in production")

        credential_dir = self.credential_directory
        if credential_dir is None:
            inherited = os.environ.get("CREDENTIALS_DIRECTORY")
            if inherited:
                credential_dir = Path(inherited)
        if self.environment == "production":
            for field_name, expected_path in _MANAGED_PRODUCTION_PATHS.items():
                configured_path = getattr(self, field_name)
                if configured_path != expected_path:
                    raise ValueError(
                        f"production {field_name} must use managed path {expected_path}"
                    )
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

    @property
    def trusted_proxy_networks(
        self,
    ) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(ipaddress.ip_network(value) for value in self.trusted_proxy_cidrs)

    @property
    def effective_trusted_hosts(self) -> list[str]:
        values = list(self.trusted_hosts)
        if self.public_base_url:
            parsed = urlsplit(self.public_base_url)
            host = parsed.hostname
            if host:
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    normalized = _normalized_dns_name(host)
                else:
                    normalized = f"[{address}]" if address.version == 6 else str(address)
                if normalized not in values:
                    values.append(normalized)
        return values

    @property
    def allowed_origin_values(self) -> frozenset[str]:
        values = set(self.allowed_browser_origins)
        if self.public_base_url:
            values.add(self.public_base_url)
        return frozenset(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
