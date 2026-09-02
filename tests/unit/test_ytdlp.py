from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from app.clients.ytdlp import (
    DownloadCancelled,
    DownloadOutputRecord,
    SourceValidationError,
    YtDlpClient,
    YtDlpError,
    _parse_result_record,
    is_curated_collection_url,
    minimal_subprocess_env,
    validate_public_media_metadata,
    validate_youtube_url,
)


def _public_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, host, ("142.250.72.14", port))]


def _private_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, host, ("127.0.0.1", port))]


def _site_local_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, host, ("fec0::1", port, 0, 0))]


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

    with pytest.raises(SourceValidationError, match="non-global"):
        validate_youtube_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            resolver=_site_local_resolver,
        )


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


def test_bounded_search_does_not_inherit_single_item_no_playlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    arguments: list[str] = []

    def capture(argv, **_kwargs):  # type: ignore[no-untyped-def]
        arguments.extend(argv)
        return {"entries": []}

    monkeypatch.setattr(client, "_run_json", capture)
    client.search("artist title", limit=3)

    assert "--no-playlist" not in arguments
    assert arguments[arguments.index("--playlist-end") + 1] == "3"
    assert arguments[-1] == "ytsearch3:artist title"


def test_collection_inspection_is_explicit_flat_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(
        str(executable),
        resolver=_public_resolver,
        enabled_providers=["bandcamp"],
    )
    arguments: list[str] = []

    def capture(argv, **_kwargs):  # type: ignore[no-untyped-def]
        arguments.extend(argv)
        return {"extractor": "bandcamp:album", "entries": [{"id": "one"}]}

    monkeypatch.setattr(client, "_run_json", capture)
    url = "https://artist.bandcamp.com/album/example"
    assert is_curated_collection_url(url)
    assert client.inspect_collection(url, limit=2)["entries"] == [{"id": "one"}]
    assert "--yes-playlist" in arguments
    assert "--flat-playlist" in arguments
    assert "--no-playlist" not in arguments
    assert arguments[arguments.index("--playlist-end") + 1] == "3"
    with pytest.raises(SourceValidationError, match="bounded item selection"):
        client.validate_url(url)


def test_collection_inspection_rejects_more_than_configured_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(
        str(executable),
        resolver=_public_resolver,
        enabled_providers=["soundcloud"],
    )
    monkeypatch.setattr(
        client,
        "_run_json",
        lambda *_args, **_kwargs: {
            "extractor": "soundcloud:set",
            "entries": [{"id": "one"}, {"id": "two"}],
        },
    )
    with pytest.raises(SourceValidationError, match="exceeds"):
        client.inspect_collection("https://soundcloud.com/artist/sets/example", limit=1)


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


def test_public_supported_requires_explicit_known_host_and_extractor(tmp_path: Path) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nprintf '{}'\n", encoding="utf-8")
    executable.chmod(0o755)
    with pytest.raises(SourceValidationError, match="requires explicit"):
        YtDlpClient(
            str(executable),
            resolver=_public_resolver,
            source_policy="public_supported",
        )
    with pytest.raises(SourceValidationError, match="not reviewed"):
        YtDlpClient(
            str(executable),
            resolver=_public_resolver,
            source_policy="public_supported",
            allowed_hosts=["www.youtube.com"],
            allowed_extractors=["unreviewed"],
        )

    client = YtDlpClient(
        str(executable),
        resolver=_public_resolver,
        source_policy="public_supported",
        enabled_providers=["youtube"],
        allowed_hosts=["www.youtube.com"],
        allowed_extractors=["youtube"],
    )
    assert client.validate_url("https://www.youtube.com/watch?v=allowed")
    with pytest.raises(SourceValidationError, match="explicitly allowed"):
        client.validate_url("https://youtu.be/not-explicitly-allowed")


@pytest.mark.parametrize("extractor", ["youtube:evil", "youtube:future_unreviewed"])
def test_probe_rejects_unreviewed_extractor_namespace_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extractor: str
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    monkeypatch.setattr(
        client,
        "_run_json",
        lambda *_args, **_kwargs: {
            "id": "candidate",
            "extractor": extractor,
            "title": "Artist - Track",
            "duration": 200,
            "acodec": "opus",
        },
    )

    with pytest.raises(SourceValidationError, match="extractor"):
        client.probe("https://www.youtube.com/watch?v=candidate")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/channel/UC123",
        "https://soundcloud.com/example",
        "https://soundcloud.com/example/likes",
        "https://artist.bandcamp.com/",
    ],
)
def test_single_item_validation_rejects_profile_and_unbounded_collection_pages(
    tmp_path: Path, url: str
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(
        str(executable),
        resolver=_public_resolver,
        enabled_providers=["youtube", "soundcloud", "bandcamp"],
    )
    with pytest.raises(SourceValidationError, match="one supported item"):
        client.validate_url(url)


@pytest.mark.parametrize(
    "metadata",
    [
        {"is_drm": True},
        {"has_drm": True},
        {"formats": [{"format_id": "protected", "has_drm": True}]},
        {"availability": "private"},
        {"availability": "unlisted"},
        {"availability": "premium_only"},
        {"availability": "subscriber_only"},
        {"availability": "needs_auth"},
    ],
)
def test_public_media_policy_rejects_protected_or_access_controlled_metadata(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(SourceValidationError):
        validate_public_media_metadata(metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"is_drm": False},
        {"availability": "public"},
        {"is_drm": False, "availability": "PUBLIC"},
    ],
)
def test_public_media_policy_preserves_genuinely_public_provider_metadata(
    metadata: dict[str, object],
) -> None:
    validate_public_media_metadata(metadata)


def test_probe_enforces_public_media_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    monkeypatch.setattr(
        client,
        "_run_json",
        lambda *_args, **_kwargs: {
            "id": "dQw4w9WgXcQ",
            "extractor": "youtube",
            "is_drm": True,
            "availability": "public",
        },
    )

    with pytest.raises(SourceValidationError, match="DRM"):
        client.probe("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_download_reprobes_policy_before_starting_media_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "output"
    output.mkdir()
    client = YtDlpClient(str(executable), resolver=_public_resolver)

    def reject_probe(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise SourceValidationError("non-public or login-gated media is not permitted")

    def unexpected_download(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("download subprocess must not start after a failed policy re-probe")

    monkeypatch.setattr(client, "probe", reject_probe)
    monkeypatch.setattr(client, "_run_download", unexpected_download)
    with pytest.raises(SourceValidationError, match="non-public"):
        client.download_audio(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            output,
            max_duration_seconds=600,
            max_media_bytes=10_000,
        )


def test_download_result_record_requires_path_extractor_and_source_identity() -> None:
    path = "/var/lib/music-agent/test-track.opus"
    assert _parse_result_record(json.dumps(path)) is None
    assert _parse_result_record(json.dumps({"filepath": path, "extractor": "youtube"})) is None
    assert _parse_result_record(
        json.dumps({"filepath": path, "extractor": "youtube", "source_id": "abc"})
    ) == DownloadOutputRecord(path, "youtube", "abc")


def test_download_rejects_result_identity_different_from_validated_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-yt-dlp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "output"
    output.mkdir()
    downloaded = output / "youtube-other.m4a"
    downloaded.write_bytes(b"audio")
    client = YtDlpClient(str(executable), resolver=_public_resolver)
    arguments: list[str] = []
    monkeypatch.setattr(
        client,
        "probe",
        lambda *_args, **_kwargs: {
            "id": "expected",
            "extractor": "youtube",
            "title": "Coldplay - Yellow",
        },
    )

    def mismatched_download(argv, **_kwargs):  # type: ignore[no-untyped-def]
        arguments.extend(argv)
        return (
            "",
            DownloadOutputRecord(str(downloaded), "youtube", "other"),
        )

    monkeypatch.setattr(client, "_run_download", mismatched_download)

    with pytest.raises(YtDlpError, match="identity"):
        client.download_audio(
            "https://www.youtube.com/watch?v=expected",
            output,
            max_duration_seconds=600,
            max_media_bytes=10_000,
        )
    assert "--max-downloads" not in arguments
    result_template = arguments[arguments.index("--print") + 1]
    assert "%(filepath)j" in result_template
    assert "%(extractor)j" in result_template
    assert "%(id)j" in result_template
