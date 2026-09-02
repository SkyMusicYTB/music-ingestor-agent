from __future__ import annotations

from app.clients.ytdlp import DownloadTimedOut, SourceValidationError, YtDlpError

_TRANSIENT_SOURCE_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary",
    "temporary failure",
    "try again",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "name or service not known",
    "could not resolve",
    "host could not be resolved",
    "no usable addresses",
    "remote end closed",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "too many requests",
    "rate limit",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def is_transient_source_error(exc: Exception) -> bool:
    if isinstance(exc, DownloadTimedOut):
        return True
    if not isinstance(exc, (YtDlpError, SourceValidationError)):
        return False
    message = str(exc).casefold()
    return any(marker in message for marker in _TRANSIENT_SOURCE_ERROR_MARKERS)


__all__ = ["is_transient_source_error"]
