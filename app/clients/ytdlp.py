from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.logging import redact
from app.sources import (
    PinnedEgressProxy,
    ProviderIdentity,
    ProviderURLPolicy,
    provider_capability,
    provider_for_extractor,
    provider_for_url,
)
from app.workers.process import (
    ProcessCancelled,
    ProcessFrameLimitExceeded,
    ProcessOutputLimitExceeded,
    ProcessTimedOut,
    run_bounded_process,
)

YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
_PROGRESS_PREFIX = "__MUSIC_AGENT_PROGRESS__"
_RESULT_PREFIX = "__MUSIC_AGENT_RESULT__"
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024


class SourceValidationError(ValueError):
    """Raised when a source URL or search query is outside the allowed policy."""


class ToolUnavailableError(RuntimeError):
    """Raised when an expected media executable cannot be resolved safely."""


class YtDlpError(RuntimeError):
    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class DownloadCancelled(YtDlpError):
    pass


class DownloadTimedOut(YtDlpError):
    pass


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    status: str
    downloaded_bytes: int | None
    total_bytes: int | None
    percent: float | None
    speed_bytes_per_second: float | None
    eta_seconds: float | None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    extractor: str | None
    source_id: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DownloadOutputRecord:
    path: str
    extractor: str
    source_id: str


Resolver = Callable[..., Sequence[tuple[Any, ...]]]
ProgressCallback = Callable[[DownloadProgress], None]


def _is_global_address(value: str) -> bool:
    # getaddrinfo may include a scope-id suffix on IPv6 literals.
    address = ipaddress.ip_address(value.split("%", 1)[0])
    return bool(
        address.is_global
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
        and not address.is_private
        and not (isinstance(address, ipaddress.IPv6Address) and address.is_site_local)
    )


def resolve_global_addresses(
    hostname: str,
    port: int = 443,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve a host and reject the entire answer if any address is non-global."""

    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise SourceValidationError("source host could not be resolved") from exc

    addresses: set[str] = set()
    for answer in answers:
        if len(answer) < 5 or not answer[4]:
            continue
        value = str(answer[4][0])
        try:
            is_global = _is_global_address(value)
        except ValueError as exc:
            raise SourceValidationError("source DNS answer was not an IP address") from exc
        if not is_global:
            raise SourceValidationError("source host resolved to a non-global address")
        addresses.add(value.split("%", 1)[0])
    if not addresses:
        raise SourceValidationError("source host returned no usable addresses")
    return tuple(sorted(addresses))


def validate_youtube_url(
    value: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Validate and normalize an HTTPS URL for a small YouTube host allowlist.

    DNS is checked here as defense in depth. The downloader resolves the name again,
    so this validation must still be paired with network egress controls in production.
    """

    return _validate_youtube_url(value, resolver=resolver, require_collection=False)


def validate_youtube_collection_url(
    value: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Validate a YouTube collection URL for bounded metadata inspection only."""

    return _validate_youtube_url(value, resolver=resolver, require_collection=True)


def _validate_youtube_url(
    value: str,
    *,
    resolver: Resolver,
    require_collection: bool,
) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise SourceValidationError("source URL must be a non-empty string up to 2048 bytes")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SourceValidationError("source URL contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SourceValidationError("source URL is malformed") from exc
    if parsed.scheme.lower() != "https":
        raise SourceValidationError("source URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise SourceValidationError("source URL must not contain credentials")
    if port not in (None, 443):
        raise SourceValidationError("source URL must use the default HTTPS port")
    if parsed.fragment:
        raise SourceValidationError("source URL fragments are not allowed")
    try:
        query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise SourceValidationError("source URL query is malformed") from exc
    has_collection = parsed.path.rstrip("/").casefold().endswith("/playlist") or any(
        key.casefold() == "list" and bool(item_value) for key, item_value in query_items
    )
    if require_collection and not has_collection:
        raise SourceValidationError("source URL does not identify a supported collection")
    if not require_collection and (
        has_collection or any(key.casefold() == "index" for key, _value in query_items)
    ):
        raise SourceValidationError("YouTube playlist parameters are not allowed")
    if not require_collection:
        normalized_path = parsed.path.rstrip("/").casefold()
        query = {key.casefold(): item_value for key, item_value in query_items}
        short_id = (
            parsed.path.strip("/") if (parsed.hostname or "").casefold() == "youtu.be" else ""
        )
        path_id = parsed.path.strip("/").split("/", 1)
        is_single = bool(
            short_id
            or (normalized_path == "/watch" and query.get("v"))
            or (
                len(path_id) == 2
                and path_id[0].casefold() in {"embed", "live", "shorts"}
                and path_id[1]
            )
        )
        if not is_single:
            raise SourceValidationError("YouTube URL does not identify one supported item")
    hostname = parsed.hostname
    if hostname is None or hostname.endswith("."):
        raise SourceValidationError("source URL has an invalid host")
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SourceValidationError("internationalized source hosts are not allowed") from exc
    normalized_host = hostname.lower()
    if normalized_host not in YOUTUBE_HOSTS:
        raise SourceValidationError("source host is not an allowed YouTube host")
    try:
        ipaddress.ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise SourceValidationError("literal IP source URLs are not allowed")
    resolve_global_addresses(normalized_host, 443, resolver=resolver)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def is_curated_collection_url(value: str) -> bool:
    """Recognize only reviewed collection URL shapes; arbitrary pages stay unsupported."""

    try:
        parsed = urlsplit(value)
        provider = provider_for_url(value)
        query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return False
    path = parsed.path.casefold().rstrip("/")
    if provider is ProviderIdentity.YOUTUBE:
        return path.endswith("/playlist") or any(
            key.casefold() == "list" and bool(item_value) for key, item_value in query_items
        )
    if provider is ProviderIdentity.SOUNDCLOUD:
        return "/sets/" in f"{path}/"
    if provider is ProviderIdentity.BANDCAMP:
        return "/album/" in f"{path}/"
    return False


def validate_search_query(value: str) -> str:
    if not isinstance(value, str):
        raise SourceValidationError("search query must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 300:
        raise SourceValidationError("search query must contain 1 to 300 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise SourceValidationError("search query contains control characters")
    return normalized


def validate_public_media_metadata(metadata: Mapping[str, Any]) -> None:
    """Reject DRM-protected or access-controlled yt-dlp metadata.

    Missing availability is tolerated because reviewed providers do not all emit the
    field for public media. When the field is present, only yt-dlp's explicit
    ``public`` value is accepted; private, unlisted, premium, subscriber, and
    authentication-gated items remain outside policy.
    """

    for key in ("is_drm", "has_drm"):
        drm = metadata.get(key)
        if drm is not None and drm is not False and drm != 0:
            raise SourceValidationError("DRM-protected media is not permitted")
    formats = metadata.get("formats")
    if isinstance(formats, list):
        for item in formats:
            if not isinstance(item, Mapping):
                continue
            drm = item.get("has_drm")
            if drm is not None and drm is not False and drm != 0:
                raise SourceValidationError("DRM-protected media is not permitted")

    availability = metadata.get("availability")
    if availability is None:
        return
    if not isinstance(availability, str) or availability.strip().casefold() != "public":
        raise SourceValidationError("non-public or login-gated media is not permitted")


def resolve_executable(value: str) -> Path:
    resolved = shutil.which(value)
    if resolved is None:
        raise ToolUnavailableError(f"required executable is unavailable: {value}")
    path = Path(resolved).resolve(strict=True)
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
        raise ToolUnavailableError(f"resolved executable is not runnable: {path}")
    return path


def minimal_subprocess_env(
    *,
    path: str | None = None,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that carries no cookies, credentials, or proxy settings."""

    source = os.environ if inherited is None else inherited
    executable_path = path if path is not None else source.get("PATH", os.defpath)
    return {
        "PATH": executable_path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
    }


class YtDlpClient:
    def __init__(
        self,
        executable: str = "yt-dlp",
        *,
        resolver: Resolver = socket.getaddrinfo,
        environment: Mapping[str, str] | None = None,
        metadata_timeout_seconds: float = 45.0,
        max_capture_bytes: int = _MAX_CAPTURE_BYTES,
        source_policy: str = "curated",
        enabled_providers: Sequence[str] = ("youtube",),
        allowed_hosts: Sequence[str] = (),
        allowed_extractors: Sequence[str] = (),
        blocked_extractors: Sequence[str] = ("generic",),
        allow_generic_extractor: bool = False,
    ) -> None:
        self.executable = resolve_executable(executable)
        self._resolver = resolver
        inherited = os.environ if environment is None else environment
        self._environment = minimal_subprocess_env(inherited=inherited)
        self._metadata_timeout_seconds = metadata_timeout_seconds
        self._max_capture_bytes = max_capture_bytes
        if source_policy not in {"curated", "public_supported"}:
            raise SourceValidationError("media source policy is unsupported")
        self._source_policy = source_policy
        self._allowed_hosts = tuple(
            dict.fromkeys(value.strip().casefold().rstrip(".") for value in allowed_hosts)
        )
        try:
            self._enabled_providers = tuple(
                dict.fromkeys(ProviderIdentity(value) for value in enabled_providers)
            )
        except ValueError as exc:
            raise SourceValidationError("an enabled media provider is unsupported") from exc
        if not self._enabled_providers:
            raise SourceValidationError("at least one media provider must be enabled")
        configured_extractors = {
            value.strip().casefold() for value in allowed_extractors if value.strip()
        }
        if source_policy == "curated":
            for provider in self._enabled_providers:
                capability = provider_capability(provider)
                if capability.acquisition:
                    configured_extractors.update(capability.extractor_aliases)
        elif not self._allowed_hosts or not configured_extractors:
            raise SourceValidationError(
                "public_supported policy requires explicit hosts and extractors"
            )
        for extractor in configured_extractors:
            extractor_provider = provider_for_extractor(extractor)
            if extractor_provider is None or extractor_provider not in self._enabled_providers:
                raise SourceValidationError(
                    "an allowed extractor is not reviewed for an enabled provider"
                )
        blocked = {value.strip().casefold() for value in blocked_extractors if value.strip()}
        if "generic" in configured_extractors and not allow_generic_extractor:
            raise SourceValidationError("the generic extractor is disabled")
        if configured_extractors & blocked:
            raise SourceValidationError("an allowed media extractor is explicitly blocked")
        self._allowed_extractors = tuple(sorted(configured_extractors))
        self._url_policy = ProviderURLPolicy()

    def _base_argv(self, *, allow_playlist: bool = False) -> list[str]:
        argv = [
            str(self.executable),
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-remote-components",
            "--use-extractors",
            ",".join(self._allowed_extractors),
            "--no-warnings",
        ]
        if not allow_playlist:
            argv.append("--no-playlist")
        return argv

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        return self.search_provider(
            query,
            provider=ProviderIdentity.YOUTUBE,
            limit=limit,
            cancel_signal=cancel_signal,
        )

    def search_provider(
        self,
        query: str,
        *,
        provider: ProviderIdentity | str,
        limit: int = 8,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        normalized = validate_search_query(query)
        if isinstance(limit, bool) or not 1 <= limit <= 10:
            raise SourceValidationError("search result limit must be between 1 and 10")
        provider_id = ProviderIdentity(provider)
        if provider_id not in self._enabled_providers:
            raise SourceValidationError("media provider is disabled")
        prefix = {
            ProviderIdentity.YOUTUBE: "ytsearch",
            ProviderIdentity.SOUNDCLOUD: "scsearch",
        }.get(provider_id)
        if prefix is None:
            raise SourceValidationError("provider does not support bounded text search")
        argv = [
            *self._base_argv(allow_playlist=True),
            "--flat-playlist",
            "--playlist-end",
            str(limit),
            "--dump-single-json",
            "--simulate",
            f"{prefix}{limit}:{normalized}",
        ]
        return self._run_json(
            argv,
            timeout_seconds=self._metadata_timeout_seconds,
            cancel_signal=cancel_signal,
        )

    def probe(self, url: str, *, cancel_signal: CancellationSignal | None = None) -> dict[str, Any]:
        validated = self.validate_url(url)
        argv = [*self._base_argv(), "--dump-single-json", "--simulate", validated]
        result = self._run_json(
            argv,
            timeout_seconds=self._metadata_timeout_seconds,
            cancel_signal=cancel_signal,
        )
        self._validate_probe_identity(validated, result)
        validate_public_media_metadata(result)
        return result

    def inspect_collection(
        self,
        url: str,
        *,
        limit: int,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        """Return a flat, bounded collection preview without downloading an item."""

        if isinstance(limit, bool) or not 1 <= limit <= 100:
            raise SourceValidationError("collection item limit must be between 1 and 100")
        validated = self.validate_collection_url(url)
        result = self._run_json(
            [
                *self._base_argv(allow_playlist=True),
                "--yes-playlist",
                "--flat-playlist",
                "--playlist-end",
                str(limit + 1),
                "--dump-single-json",
                "--simulate",
                validated,
            ],
            timeout_seconds=self._metadata_timeout_seconds,
            cancel_signal=cancel_signal,
        )
        self._validate_probe_identity(validated, result)
        entries = result.get("entries")
        if not isinstance(entries, list):
            raise SourceValidationError("collection metadata did not contain a bounded item list")
        if len(entries) > limit:
            raise SourceValidationError("collection exceeds the configured item limit")
        for entry in entries:
            if isinstance(entry, Mapping):
                validate_public_media_metadata(entry)
        return result

    def validate_url(self, url: str) -> str:
        if is_curated_collection_url(url):
            raise SourceValidationError("collection URLs require bounded item selection")
        return self._validate_provider_url(url, allow_collection=False)

    def validate_collection_url(self, url: str) -> str:
        if not is_curated_collection_url(url):
            raise SourceValidationError("source URL does not identify a supported collection")
        return self._validate_provider_url(url, allow_collection=True)

    def _validate_provider_url(self, url: str, *, allow_collection: bool) -> str:
        provider = provider_for_url(url)
        if provider is None or provider not in self._enabled_providers:
            raise SourceValidationError("source host is not an enabled curated provider")
        if provider is ProviderIdentity.YOUTUBE:
            validated = (
                validate_youtube_collection_url(url, resolver=self._resolver)
                if allow_collection
                else validate_youtube_url(url, resolver=self._resolver)
            )
        else:
            validation = self._url_policy.validate(url, provider=provider)
            if not validation.allowed:
                raise SourceValidationError(validation.reason_code)
            parsed_input = urlsplit(url)
            assert parsed_input.hostname is not None
            if not allow_collection and not _is_curated_single_item_path(
                parsed_input.path,
                provider,
            ):
                raise SourceValidationError("source URL does not identify one supported item")
            try:
                normalized_hostname = (
                    parsed_input.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
                )
            except UnicodeError as exc:
                raise SourceValidationError("source host is not valid IDNA") from exc
            resolve_global_addresses(normalized_hostname, 443, resolver=self._resolver)
            validated = urlunsplit(
                (
                    "https",
                    normalized_hostname,
                    parsed_input.path or "/",
                    parsed_input.query,
                    "",
                )
            )
        parsed = urlsplit(validated)
        assert parsed.hostname is not None
        if self._source_policy == "public_supported" and not _host_matches_any(
            parsed.hostname, self._allowed_hosts
        ):
            raise SourceValidationError("source host is not explicitly allowed")
        return validated

    def _validate_probe_identity(self, url: str, metadata: Mapping[str, Any]) -> None:
        expected = provider_for_url(url)
        extractor = _optional_string(metadata.get("extractor")) or _optional_string(
            metadata.get("extractor_key")
        )
        if extractor is None:
            raise SourceValidationError("media probe returned no extractor identity")
        normalized = extractor.strip().casefold()
        actual = provider_for_extractor(normalized)
        if actual is None or actual is not expected or not self._extractor_allowed(normalized):
            raise SourceValidationError("media extractor did not match the validated provider")
        if normalized == "generic" or normalized in {"generic", "generic:default"}:
            raise SourceValidationError("generic media extraction is prohibited")

    def _extractor_allowed(self, extractor: str) -> bool:
        return extractor in self._allowed_extractors

    def download_audio(
        self,
        url: str,
        output_directory: Path,
        *,
        max_duration_seconds: int,
        max_media_bytes: int = 1024 * 1024 * 1024,
        timeout_seconds: float = 1800.0,
        progress_callback: ProgressCallback | None = None,
        cancel_signal: CancellationSignal | None = None,
    ) -> DownloadResult:
        validated = self.validate_url(url)
        output_directory = output_directory.resolve(strict=True)
        output_stat = output_directory.stat()
        if not stat.S_ISDIR(output_stat.st_mode) or output_directory.is_symlink():
            raise SourceValidationError("download destination must be a real directory")
        if isinstance(max_duration_seconds, bool) or max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")
        if isinstance(max_media_bytes, bool) or max_media_bytes <= 0:
            raise ValueError("max_media_bytes must be positive")

        argv = [
            *self._base_argv(),
            "--format",
            "bestaudio/best",
            "--extract-audio",
            "--audio-format",
            "best",
            "--paths",
            f"home:{output_directory}",
            "--paths",
            f"temp:{output_directory}",
            "--output",
            "%(extractor)s-%(id)s.%(ext)s",
            "--no-overwrites",
            "--max-filesize",
            str(max_media_bytes),
            "--match-filter",
            f"duration <= {int(max_duration_seconds)}",
            "--newline",
            "--progress",
            "--progress-template",
            f"download:{_PROGRESS_PREFIX}%(progress)j",
            "--print",
            (
                f'after_move:{_RESULT_PREFIX}{{"filepath":%(filepath)j,'
                '"extractor":%(extractor)j,"source_id":%(id)j}'
            ),
            "--no-simulate",
            validated,
        ]
        metadata = self.probe(validated, cancel_signal=cancel_signal)
        lines, output_record = self._run_download(
            argv,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            cancel_signal=cancel_signal,
        )
        if output_record is None:
            raise YtDlpError(
                f"yt-dlp completed without a complete result record: {redact(lines[-2000:])}"
            )
        expected_extractor = _optional_string(metadata.get("extractor")) or _optional_string(
            metadata.get("extractor_key")
        )
        expected_source_id = _optional_string(metadata.get("id"))
        if (
            expected_extractor is None
            or expected_source_id is None
            or output_record.extractor.casefold() != expected_extractor.casefold()
            or output_record.source_id != expected_source_id
        ):
            raise YtDlpError("downloaded source identity did not match the validated probe")
        self._validate_probe_identity(
            validated,
            {"extractor": output_record.extractor, "id": output_record.source_id},
        )
        candidate = Path(output_record.path)
        if not candidate.is_absolute():
            candidate = output_directory / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(output_directory)
        except (OSError, ValueError) as exc:
            raise YtDlpError("yt-dlp returned a path outside its staging directory") from exc
        file_stat = resolved.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or resolved.is_symlink():
            raise YtDlpError("yt-dlp result was not a regular file")
        return DownloadResult(
            path=resolved,
            extractor=output_record.extractor,
            source_id=output_record.source_id,
            metadata=metadata,
        )

    def _run_json(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        output = self._run_capture(
            argv, timeout_seconds=timeout_seconds, cancel_signal=cancel_signal
        )
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise YtDlpError("yt-dlp returned malformed metadata JSON") from exc
        if not isinstance(value, dict):
            raise YtDlpError("yt-dlp metadata response was not an object")
        return value

    def _run_capture(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        cancel_signal: CancellationSignal | None = None,
    ) -> str:
        try:
            with PinnedEgressProxy(resolver=self._resolver) as egress:
                result = run_bounded_process(
                    _with_proxy(argv, egress.url),
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                    cancel_signal=cancel_signal,
                    stdout_limit=self._max_capture_bytes,
                    stderr_limit=self._max_capture_bytes,
                )
        except ProcessCancelled as exc:
            raise DownloadCancelled("yt-dlp metadata request was cancelled") from exc
        except ProcessTimedOut as exc:
            raise DownloadTimedOut("yt-dlp metadata request timed out") from exc
        except (ProcessOutputLimitExceeded, ProcessFrameLimitExceeded) as exc:
            raise YtDlpError("yt-dlp metadata output exceeded the configured limit") from exc
        decoded_stdout = result.stdout.decode("utf-8", errors="replace")
        decoded_stderr = result.stderr_tail.decode("utf-8", errors="replace")
        if result.returncode != 0:
            detail = decoded_stderr[-4000:] or decoded_stdout[-4000:]
            raise YtDlpError(
                f"yt-dlp metadata request failed: {redact(detail)}",
                returncode=result.returncode,
            )
        return decoded_stdout

    def _run_download(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        progress_callback: ProgressCallback | None,
        cancel_signal: CancellationSignal | None,
    ) -> tuple[str, DownloadOutputRecord | None]:
        output_record: DownloadOutputRecord | None = None

        def frame(_stream_name: str, raw_frame: bytes) -> None:
            nonlocal output_record
            stripped = raw_frame.decode("utf-8", errors="replace").strip()
            if stripped.startswith(_PROGRESS_PREFIX):
                progress = _parse_progress(stripped.removeprefix(_PROGRESS_PREFIX))
                if progress is not None and progress_callback is not None:
                    progress_callback(progress)
            elif stripped.startswith(_RESULT_PREFIX):
                output_record = _parse_result_record(stripped.removeprefix(_RESULT_PREFIX))

        try:
            with PinnedEgressProxy(resolver=self._resolver) as egress:
                result = run_bounded_process(
                    _with_proxy(argv, egress.url),
                    environment=self._environment,
                    timeout_seconds=timeout_seconds,
                    cancel_signal=cancel_signal,
                    stdout_limit=self._max_capture_bytes,
                    stderr_limit=self._max_capture_bytes,
                    on_frame=frame,
                )
        except ProcessCancelled as exc:
            raise DownloadCancelled("download was cancelled") from exc
        except ProcessTimedOut as exc:
            raise DownloadTimedOut("download exceeded its time limit") from exc
        except (ProcessOutputLimitExceeded, ProcessFrameLimitExceeded) as exc:
            raise YtDlpError("yt-dlp download output exceeded the configured limit") from exc
        combined = (result.stdout + b"\n" + result.stderr_tail).decode("utf-8", errors="replace")
        if result.returncode != 0 and not (result.returncode == 101 and output_record is not None):
            raise YtDlpError(
                f"yt-dlp download failed: {redact(combined[-4000:])}",
                returncode=result.returncode,
            )
        return combined, output_record


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _host_matches_any(hostname: str, patterns: Sequence[str]) -> bool:
    normalized = hostname.rstrip(".").casefold()
    return any(
        normalized == pattern or (pattern.startswith("*.") and normalized.endswith(pattern[1:]))
        for pattern in patterns
    )


def _is_curated_single_item_path(path: str, provider: ProviderIdentity) -> bool:
    parts = [part for part in path.casefold().split("/") if part]
    if provider is ProviderIdentity.BANDCAMP:
        return len(parts) == 2 and parts[0] == "track" and bool(parts[1])
    if provider is ProviderIdentity.SOUNDCLOUD:
        reserved_collections = {
            "albums",
            "likes",
            "popular-tracks",
            "reposts",
            "sets",
            "tracks",
        }
        return len(parts) == 2 and parts[1] not in reserved_collections
    return False


def _with_proxy(argv: Sequence[str], proxy_url: str) -> list[str]:
    if not argv:
        raise ValueError("yt-dlp argument vector cannot be empty")
    return [str(argv[0]), "--proxy", proxy_url, *(str(item) for item in argv[1:])]


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_progress(payload: str) -> DownloadProgress | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    downloaded = _optional_int(value.get("downloaded_bytes"))
    total = _optional_int(value.get("total_bytes") or value.get("total_bytes_estimate"))
    percent: float | None = None
    if downloaded is not None and total is not None and total > 0:
        percent = min(100.0, max(0.0, downloaded / total * 100.0))
    return DownloadProgress(
        status=_optional_string(value.get("status")) or "downloading",
        downloaded_bytes=downloaded,
        total_bytes=total,
        percent=percent,
        speed_bytes_per_second=_optional_float(value.get("speed")),
        eta_seconds=_optional_float(value.get("eta")),
    )


def _parse_result_record(payload: str) -> DownloadOutputRecord | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    path = _optional_string(value.get("filepath"))
    extractor = _optional_string(value.get("extractor"))
    source_id = _optional_string(value.get("source_id"))
    if path is None or extractor is None or source_id is None:
        return None
    return DownloadOutputRecord(path=path, extractor=extractor, source_id=source_id)
