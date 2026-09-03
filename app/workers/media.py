from __future__ import annotations

import json
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.clients.ytdlp import (
    CancellationSignal,
    DownloadCancelled,
    ToolUnavailableError,
    minimal_subprocess_env,
    resolve_executable,
)
from app.logging import redact
from app.workers.process import (
    ProcessCancelled,
    ProcessFrameLimitExceeded,
    ProcessOutputLimitExceeded,
    ProcessTimedOut,
    run_bounded_process,
)


class MediaValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: Path
    codec: str
    container: tuple[str, ...]
    duration_seconds: float
    bitrate: int | None


class MediaProcessor:
    def __init__(
        self,
        *,
        ffprobe: str = "ffprobe",
        ffmpeg: str = "ffmpeg",
        environment: dict[str, str] | None = None,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.ffprobe = resolve_executable(ffprobe)
        self.ffmpeg = resolve_executable(ffmpeg)
        self.environment = minimal_subprocess_env(inherited=environment)
        self.timeout_seconds = timeout_seconds

    def normalize_and_verify(
        self,
        path: Path,
        *,
        max_duration_seconds: int,
        allow_lossy_transcode: bool,
        cancel_signal: CancellationSignal | None = None,
        allow_attached_art: bool = False,
    ) -> MediaProbe:
        probe = self.inspect(
            path,
            max_duration_seconds=max_duration_seconds,
            cancel_signal=cancel_signal,
            allow_attached_art=allow_attached_art,
        )
        extension: str | None = None
        codec_args = ["-c:a", "copy"]
        format_name: str | None = None
        if probe.codec == "opus":
            if path.suffix.casefold() in {".opus", ".ogg"} and "ogg" in probe.container:
                return probe
            extension, format_name = ".opus", "opus"
        elif probe.codec == "aac":
            if path.suffix.casefold() in {".m4a", ".mp4"} and any(
                item in probe.container for item in ("mov", "mp4", "m4a")
            ):
                return probe
            extension, format_name = ".m4a", "ipod"
        elif probe.codec == "mp3":
            if path.suffix.casefold() == ".mp3" and "mp3" in probe.container:
                return probe
            extension, format_name = ".mp3", "mp3"
        elif probe.codec == "flac":
            if path.suffix.casefold() == ".flac" and "flac" in probe.container:
                return probe
            extension, format_name = ".flac", "flac"
        elif probe.codec == "vorbis" and allow_lossy_transcode:
            extension, format_name = ".opus", "opus"
            codec_args = ["-c:a", "libopus", "-b:a", "160k"]
        else:
            raise MediaValidationError(
                f"unsupported audio codec {probe.codec!r}; lossy transcoding is disabled"
            )
        assert extension is not None and format_name is not None
        normalized = path.with_name(f".normalized-{uuid.uuid4().hex}{extension}")
        argv = [
            str(self.ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            *codec_args,
            "-f",
            format_name,
            str(normalized),
        ]
        self._run(argv, cancel_signal=cancel_signal)
        try:
            verified = self.inspect(
                normalized,
                max_duration_seconds=max_duration_seconds,
                cancel_signal=cancel_signal,
                allow_attached_art=allow_attached_art,
            )
        except Exception:
            normalized.unlink(missing_ok=True)
            raise
        if verified.codec not in {"opus", "aac", "mp3", "flac"}:
            normalized.unlink(missing_ok=True)
            raise MediaValidationError("normalized output used an unexpected codec")
        path.unlink()
        return verified

    def inspect(
        self,
        path: Path,
        *,
        max_duration_seconds: int,
        cancel_signal: CancellationSignal | None = None,
        allow_attached_art: bool = False,
    ) -> MediaProbe:
        file_stat = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
            raise MediaValidationError("media input must be a regular non-symlink file")
        argv = [
            str(self.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration,bit_rate:stream=codec_type,codec_name,duration:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ]
        output = self._run(argv, cancel_signal=cancel_signal)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise MediaValidationError("ffprobe returned malformed JSON") from exc
        return parse_probe_payload(
            path,
            payload,
            max_duration_seconds=max_duration_seconds,
            allow_attached_art=allow_attached_art,
        )

    def _run(
        self,
        argv: list[str],
        *,
        cancel_signal: CancellationSignal | None = None,
    ) -> str:
        try:
            result = run_bounded_process(
                argv,
                environment=self.environment,
                timeout_seconds=self.timeout_seconds,
                cancel_signal=cancel_signal,
                stdout_limit=1024 * 1024,
                stderr_limit=1024 * 1024,
            )
        except ProcessCancelled as exc:
            raise DownloadCancelled("media processing was cancelled") from exc
        except ProcessTimedOut as exc:
            raise MediaValidationError("media subprocess timed out") from exc
        except (ProcessOutputLimitExceeded, ProcessFrameLimitExceeded) as exc:
            raise MediaValidationError("media subprocess output exceeded its bound") from exc
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr_tail.decode("utf-8", errors="replace")
        if result.returncode != 0:
            raise MediaValidationError(f"media subprocess failed: {redact(stderr[-2000:])}")
        return stdout


def parse_probe_payload(
    path: Path,
    payload: object,
    *,
    max_duration_seconds: int,
    allow_attached_art: bool = False,
) -> MediaProbe:
    if not isinstance(payload, dict):
        raise MediaValidationError("ffprobe payload is not an object")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaValidationError("ffprobe payload has no streams")
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(audio_streams) != 1:
        raise MediaValidationError("media must contain exactly one audio stream")
    if any(
        isinstance(stream, dict)
        and stream.get("codec_type") == "video"
        and not (
            allow_attached_art
            and stream.get("codec_name") in {"mjpeg", "png"}
            and isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("attached_pic") == 1
        )
        for stream in streams
    ):
        raise MediaValidationError("media must not contain a video stream")
    codec = audio_streams[0].get("codec_name")
    if not isinstance(codec, str) or not codec:
        raise MediaValidationError("audio stream has no codec")
    format_value = payload.get("format")
    if not isinstance(format_value, dict):
        raise MediaValidationError("ffprobe payload has no format")
    names = format_value.get("format_name")
    if not isinstance(names, str) or not names:
        raise MediaValidationError("media container could not be identified")
    duration = _positive_float(format_value.get("duration")) or _positive_float(
        audio_streams[0].get("duration")
    )
    if duration is None or duration > max_duration_seconds:
        raise MediaValidationError("media duration is missing or exceeds the configured limit")
    bitrate_value = format_value.get("bit_rate")
    bitrate_text = str(bitrate_value)
    bitrate = int(bitrate_text) if bitrate_text.isdigit() else None
    return MediaProbe(
        path=path.resolve(strict=True),
        codec=codec.casefold(),
        container=tuple(item.casefold() for item in names.split(",") if item),
        duration_seconds=duration,
        bitrate=bitrate,
    )


def _positive_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


__all__ = [
    "MediaProbe",
    "MediaProcessor",
    "MediaValidationError",
    "ToolUnavailableError",
    "parse_probe_payload",
]
