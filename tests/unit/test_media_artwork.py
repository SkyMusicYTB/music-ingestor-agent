from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from app.clients.ytdlp import SourceValidationError
from app.db.models import ArtworkCache
from app.services.artwork import (
    Artwork,
    ArtworkCacheService,
    ArtworkError,
    ArtworkFetchResult,
    ArtworkNegativeCache,
    ArtworkNotFound,
    artwork_as_jpeg,
    normalize_artwork,
)
from app.workers.processor import DownloadJobProcessor


def test_artwork_is_decoded_and_reencoded_without_input_metadata() -> None:
    source = io.BytesIO()
    Image.new("RGB", (32, 24), (20, 40, 60)).save(source, format="PNG", comment=b"secret")
    result = normalize_artwork(source.getvalue())
    assert result.mime_type == "image/jpeg"
    assert (result.width, result.height) == (32, 24)
    assert result.data.startswith(b"\xff\xd8")
    assert b"secret" not in result.data


def test_artwork_rejects_non_images_and_pixel_bombs() -> None:
    with pytest.raises(ArtworkError):
        normalize_artwork(b"not an image")
    source = io.BytesIO()
    Image.new("RGB", (20, 20), "black").save(source, format="PNG")
    with pytest.raises(ArtworkError, match="pixel"):
        normalize_artwork(source.getvalue(), max_pixels=100)


def test_artwork_is_bounded_to_1200_pixels() -> None:
    source = io.BytesIO()
    Image.new("RGB", (1600, 800), "navy").save(source, format="JPEG")
    result = normalize_artwork(source.getvalue())
    assert (result.width, result.height) == (1200, 600)


def test_png_alpha_is_composited_into_real_jpeg_sidecar() -> None:
    source = io.BytesIO()
    Image.new("RGBA", (16, 16), (255, 0, 0, 128)).save(source, format="PNG")
    artwork = normalize_artwork(source.getvalue())
    assert artwork.mime_type == "image/png"
    jpeg = artwork_as_jpeg(artwork)
    assert jpeg.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.mode == "RGB"


class _ArtworkQueue:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, str]] = []

    def set_progress(self, *_args: object, **_kwargs: object) -> None:
        return None

    def add_warning(self, _lease: object, *, code: str, message: str) -> None:
        self.warnings.append((code, message))


class _ArtworkMonitor:
    def raise_if_unusable(self) -> None:
        return None


class _RecordingFetcher:
    def __init__(self, succeeds_at: str) -> None:
        self.succeeds_at = succeeds_at
        self.urls: list[str] = []

    def fetch(self, url: str) -> Artwork:
        self.urls.append(url)
        if url != self.succeeds_at:
            raise ArtworkError("not found")
        return Artwork(b"\xff\xd8jpeg", "image/jpeg", 10, 10, "0" * 64)


def _artwork_processor(fetcher: _RecordingFetcher) -> DownloadJobProcessor:
    processor = DownloadJobProcessor.__new__(DownloadJobProcessor)
    processor.artwork_fetcher = fetcher
    processor.queue = _ArtworkQueue()
    return processor


def test_artwork_uses_server_derived_caa_order_and_ignores_snapshot_url() -> None:
    group_url = (
        "https://coverartarchive.org/release-group/22222222-2222-2222-2222-222222222222/front-1200"
    )
    fetcher = _RecordingFetcher(group_url)
    processor = _artwork_processor(fetcher)
    lease = SimpleNamespace(
        job_id="11111111-1111-1111-1111-111111111111",
        approved_snapshot={"artwork_url": "https://attacker.invalid/model-art"},
    )
    values = {
        "release_mbid": "11111111-1111-1111-1111-111111111111",
        "release_group_mbid": "22222222-2222-2222-2222-222222222222",
    }

    result = processor._fetch_artwork(  # type: ignore[arg-type]
        lease,
        _ArtworkMonitor(),
        {"thumbnail": "https://i.ytimg.com/vi/source/maxresdefault.jpg"},
        values,
    )

    assert result is not None
    assert fetcher.urls == [
        "https://coverartarchive.org/release/11111111-1111-1111-1111-111111111111/front-1200",
        group_url,
    ]
    assert all("attacker.invalid" not in url for url in fetcher.urls)


def test_artwork_falls_back_to_provider_thumbnail_after_caa(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thumbnail = "https://i.ytimg.com/vi/source/maxresdefault.jpg"
    fetcher = _RecordingFetcher(thumbnail)
    processor = _artwork_processor(fetcher)
    lease = SimpleNamespace(
        job_id="11111111-1111-1111-1111-111111111111",
        approved_snapshot={},
    )

    result = processor._fetch_artwork(  # type: ignore[arg-type]
        lease,
        _ArtworkMonitor(),
        {"thumbnail": thumbnail},
        {"release_mbid": "11111111-1111-1111-1111-111111111111"},
    )

    assert result is not None and fetcher.urls[-1] == thumbnail
    assert "validated YouTube thumbnail was embedded" in caplog.text
    assert processor.queue.warnings[0][0] == "youtube_thumbnail_artwork"


def test_artwork_cache_or_database_failure_remains_optional() -> None:
    class BrokenFetcher:
        def fetch(self, _url: str) -> Artwork:
            raise RuntimeError("cache database unavailable")

    processor = _artwork_processor(BrokenFetcher())  # type: ignore[arg-type]
    lease = SimpleNamespace(
        job_id="11111111-1111-1111-1111-111111111111",
        approved_snapshot={},
    )

    result = processor._fetch_artwork(  # type: ignore[arg-type]
        lease,
        _ArtworkMonitor(),
        {},
        {"release_mbid": "11111111-1111-1111-1111-111111111111"},
    )

    assert result is None


class _CacheFetcher:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def fetch_with_metadata(self, url: str) -> ArtworkFetchResult:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        data = b"\xff\xd8normalized-jpeg"
        artwork = Artwork(
            data,
            "image/jpeg",
            1200,
            1200,
            hashlib.sha256(data).hexdigest(),
        )
        return ArtworkFetchResult(
            artwork,
            url,
            '"etag-value"',
            "Tue, 01 Sep 2026 12:00:00 GMT",
        )


def test_persistent_artwork_cache_reuses_blob_and_records_validators(
    session_factory, tmp_path
) -> None:
    url = "https://coverartarchive.org/release/11111111-1111-1111-1111-111111111111/front-1200"
    fetcher = _CacheFetcher()
    cache = ArtworkCacheService(
        session_factory,
        tmp_path / "artwork-cache",
        fetcher,  # type: ignore[arg-type]
    )

    first = cache.fetch(url)
    second = cache.fetch(url)

    assert first == second and fetcher.calls == 1
    with session_factory() as session:
        row = session.scalar(select(ArtworkCache))
        assert row is not None
        assert row.cache_key == "caa-release:11111111-1111-1111-1111-111111111111"
        assert row.status == "ok" and row.etag == '"etag-value"'
        assert row.last_modified == "Tue, 01 Sep 2026 12:00:00 GMT"
        assert row.relative_path is not None
        assert (tmp_path / "artwork-cache" / row.relative_path).read_bytes() == first.data


def test_concurrent_artwork_cache_insert_converges_on_one_row(session_factory, tmp_path) -> None:
    url = "https://coverartarchive.org/release/44444444-4444-4444-4444-444444444444/front-1200"
    fetcher = _CacheFetcher()
    cache = ArtworkCacheService(session_factory, tmp_path / "concurrent-artwork", fetcher)  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _value: cache.fetch(url), range(2)))

    assert results[0].sha256 == results[1].sha256
    with session_factory() as session:
        assert len(list(session.scalars(select(ArtworkCache)))) == 1


@pytest.mark.parametrize(
    ("url", "failure", "status"),
    [
        (
            "https://coverartarchive.org/release/22222222-2222-2222-2222-222222222222/front-1200",
            ArtworkNotFound("missing"),
            "not_found",
        ),
        (
            "https://attacker.invalid/model-art",
            SourceValidationError("artwork host is not allowlisted"),
            "unsafe",
        ),
    ],
)
def test_artwork_cache_negative_caches_not_found_and_unsafe(
    session_factory, tmp_path, url: str, failure: Exception, status: str
) -> None:
    fetcher = _CacheFetcher(failure=failure)
    cache = ArtworkCacheService(
        session_factory,
        tmp_path / "artwork-cache",
        fetcher,  # type: ignore[arg-type]
    )

    with pytest.raises(type(failure)):
        cache.fetch(url)
    with pytest.raises(ArtworkNegativeCache):
        cache.fetch(url)

    assert fetcher.calls == 1
    with session_factory() as session:
        row = session.scalar(select(ArtworkCache))
        assert row is not None and row.status == status and row.expires_at is not None
