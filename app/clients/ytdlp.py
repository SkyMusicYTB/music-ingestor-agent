from __future__ import annotations

import ipaddress
import json
import os
import queue
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.logging import redact

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
    if parsed.path.rstrip("/").casefold().endswith("/playlist"):
        raise SourceValidationError("YouTube playlist URLs are not allowed")
    try:
        query_items = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise SourceValidationError("source URL query is malformed") from exc
    if any(key.casefold() in {"list", "index"} for key, _value in query_items):
        raise SourceValidationError("YouTube playlist parameters are not allowed")
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


def validate_search_query(value: str) -> str:
    if not isinstance(value, str):
        raise SourceValidationError("search query must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 300:
        raise SourceValidationError("search query must contain 1 to 300 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise SourceValidationError("search query contains control characters")
    return normalized


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


def terminate_process_group(process: subprocess.Popen[Any], grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - the service target is Ubuntu
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - the service target is Ubuntu
            process.kill()
    except ProcessLookupError:
        return
    process.wait(timeout=max(1.0, grace_seconds))


class YtDlpClient:
    def __init__(
        self,
        executable: str = "yt-dlp",
        *,
        resolver: Resolver = socket.getaddrinfo,
        environment: Mapping[str, str] | None = None,
        metadata_timeout_seconds: float = 45.0,
        max_capture_bytes: int = _MAX_CAPTURE_BYTES,
    ) -> None:
        self.executable = resolve_executable(executable)
        self._resolver = resolver
        inherited = os.environ if environment is None else environment
        self._environment = minimal_subprocess_env(inherited=inherited)
        self._metadata_timeout_seconds = metadata_timeout_seconds
        self._max_capture_bytes = max_capture_bytes

    def _base_argv(self) -> list[str]:
        return [
            str(self.executable),
            "--ignore-config",
            "--no-plugin-dirs",
            "--no-remote-components",
            "--no-playlist",
            "--use-extractors",
            "youtube,youtube:search",
            "--no-warnings",
        ]

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        cancel_signal: CancellationSignal | None = None,
    ) -> dict[str, Any]:
        normalized = validate_search_query(query)
        if isinstance(limit, bool) or not 1 <= limit <= 10:
            raise SourceValidationError("search result limit must be between 1 and 10")
        argv = [
            *self._base_argv(),
            "--flat-playlist",
            "--dump-single-json",
            "--simulate",
            f"ytsearch{limit}:{normalized}",
        ]
        return self._run_json(
            argv,
            timeout_seconds=self._metadata_timeout_seconds,
            cancel_signal=cancel_signal,
        )

    def probe(self, url: str, *, cancel_signal: CancellationSignal | None = None) -> dict[str, Any]:
        validated = self.validate_url(url)
        argv = [*self._base_argv(), "--dump-single-json", "--simulate", validated]
        return self._run_json(
            argv,
            timeout_seconds=self._metadata_timeout_seconds,
            cancel_signal=cancel_signal,
        )

    def validate_url(self, url: str) -> str:
        return validate_youtube_url(url, resolver=self._resolver)

    def download_audio(
        self,
        url: str,
        output_directory: Path,
        *,
        max_duration_seconds: int,
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
            "--max-downloads",
            "1",
            "--match-filter",
            f"duration <= {int(max_duration_seconds)}",
            "--newline",
            "--progress",
            "--progress-template",
            f"download:{_PROGRESS_PREFIX}%(progress)j",
            "--print",
            f"after_move:{_RESULT_PREFIX}%(filepath)j",
            "--no-simulate",
            validated,
        ]
        metadata = self.probe(validated, cancel_signal=cancel_signal)
        lines, final_path = self._run_download(
            argv,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            cancel_signal=cancel_signal,
        )
        if final_path is None:
            raise YtDlpError(f"yt-dlp completed without a final path: {redact(lines[-2000:])}")
        candidate = Path(final_path)
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
            extractor=_optional_string(metadata.get("extractor")),
            source_id=_optional_string(metadata.get("id")),
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
        process = subprocess.Popen(  # noqa: S603 - argv is fixed and shell is disabled
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            env=self._environment,
            start_new_session=True,
        )
        started = time.monotonic()
        while True:
            if cancel_signal is not None and cancel_signal.is_set():
                terminate_process_group(process)
                process.communicate()
                raise DownloadCancelled("yt-dlp metadata request was cancelled")
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                terminate_process_group(process)
                process.communicate()
                raise DownloadTimedOut("yt-dlp metadata request timed out")
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if len(stdout) > self._max_capture_bytes or len(stderr) > self._max_capture_bytes:
            raise YtDlpError("yt-dlp metadata output exceeded the configured limit")
        decoded_stdout = stdout.decode("utf-8", errors="replace")
        decoded_stderr = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            detail = decoded_stderr[-4000:] or decoded_stdout[-4000:]
            raise YtDlpError(
                f"yt-dlp metadata request failed: {redact(detail)}",
                returncode=process.returncode,
            )
        return decoded_stdout

    def _run_download(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        progress_callback: ProgressCallback | None,
        cancel_signal: CancellationSignal | None,
    ) -> tuple[str, str | None]:
        process = subprocess.Popen(  # noqa: S603 - argv is fixed and shell is disabled
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            env=self._environment,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        line_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def read_stream(name: str, stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    line_queue.put((name, line))
            finally:
                line_queue.put((name, None))

        readers = [
            threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        started = time.monotonic()
        closed_streams: set[str] = set()
        captured: list[str] = []
        captured_size = 0
        final_path: str | None = None
        try:
            while process.poll() is None or len(closed_streams) < 2:
                if cancel_signal is not None and cancel_signal.is_set():
                    terminate_process_group(process)
                    raise DownloadCancelled("download was cancelled")
                if time.monotonic() - started > timeout_seconds:
                    terminate_process_group(process)
                    raise DownloadTimedOut("download exceeded its time limit")
                try:
                    stream_name, line = line_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    closed_streams.add(stream_name)
                    continue
                encoded_size = len(line.encode("utf-8", errors="replace"))
                captured_size += encoded_size
                if captured_size <= self._max_capture_bytes:
                    captured.append(line)
                elif captured_size - encoded_size <= self._max_capture_bytes:
                    captured.append("[yt-dlp output truncated]\n")
                stripped = line.strip()
                if stripped.startswith(_PROGRESS_PREFIX):
                    progress = _parse_progress(stripped.removeprefix(_PROGRESS_PREFIX))
                    if progress is not None and progress_callback is not None:
                        progress_callback(progress)
                elif stripped.startswith(_RESULT_PREFIX):
                    final_path = _parse_result_path(stripped.removeprefix(_RESULT_PREFIX))
        finally:
            if process.poll() is None:
                terminate_process_group(process)
            for reader in readers:
                reader.join(timeout=1.0)
            process.stdout.close()
            process.stderr.close()
        combined = "".join(captured)
        if process.returncode != 0:
            raise YtDlpError(
                f"yt-dlp download failed: {redact(combined[-4000:])}",
                returncode=process.returncode,
            )
        return combined, final_path


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


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


def _parse_result_path(payload: str) -> str | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) and value else None
