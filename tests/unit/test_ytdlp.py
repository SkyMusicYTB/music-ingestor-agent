from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from app.clients.ytdlp import (
    DownloadCancelled,
    SourceValidationError,
    YtDlpClient,
    minimal_subprocess_env,
    validate_youtube_url,
)


def _public_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, host, ("142.250.72.14", port))]


def _private_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, host, ("127.0.0.1", port))]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
        "https://user@www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123",
        "https://youtu.be/dQw4w9WgXcQ#fragment",
    ],
)
def test_youtube_url_policy_rejects_non_allowlisted_urls(url: str) -> None:
    with pytest.raises(SourceValidationError):
        validate_youtube_url(url, resolver=_public_resolver)


def test_youtube_url_policy_requires_only_global_dns_answers() -> None:
    with pytest.raises(SourceValidationError, match="non-global"):
        validate_youtube_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            resolver=_private_resolver,
        )
    normalized = validate_youtube_url("https://youtu.be/dQw4w9WgXcQ", resolver=_public_resolver)
    assert normalized == "https://youtu.be/dQw4w9WgXcQ"


def test_minimal_environment_drops_credentials_and_proxies() -> None:
    environment = minimal_subprocess_env(
        inherited={
            "PATH": "/trusted/bin:/usr/bin",
            "HOME": "/secret",
            "HTTPS_PROXY": "http://proxy.invalid",
            "OPENAI_API_KEY": "secret",
        }
    )
    assert environment["PATH"] == "/trusted/bin:/usr/bin"
    assert "HOME" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "OPENAI_API_KEY" not in environment


def test_search_uses_bounded_ytsearch_without_a_shell(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nprintf '%s' '{\"entries\":[]}'\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(
        str(executable), resolver=_public_resolver, environment={"PATH": "/usr/bin:/bin"}
    )
    result = client.search("artist; $(touch should-not-run)", limit=3)
    assert result == {"entries": []}
    assert not (tmp_path / "should-not-run").exists()


def test_search_rejects_unbounded_result_counts(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nprintf '{}'\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    with pytest.raises(SourceValidationError):
        client.search("query", limit=11)


def test_metadata_output_must_be_a_json_object(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nprintf '%s' '[]'\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    with pytest.raises(Exception, match="not an object"):
        client.search("query")


def test_metadata_probe_honors_cancellation_signal(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nsleep 10\nprintf '{}\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(DownloadCancelled):
        client.search("query", cancel_signal=cancelled)
