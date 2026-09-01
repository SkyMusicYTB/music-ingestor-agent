from __future__ import annotations

import hashlib
import io
import os
import re
import socket
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.clients.ytdlp import Resolver, SourceValidationError, resolve_global_addresses
from app.db.models import ArtworkCache

DEFAULT_ARTWORK_HOSTS = (
    "coverartarchive.org",
    "archive.org",
    ".archive.org",
    "i.ytimg.com",
    "img.youtube.com",
)


class ArtworkError(RuntimeError):
    pass


class ArtworkNotFound(ArtworkError):
    pass


class ArtworkNegativeCache(ArtworkError):
    pass


@dataclass(frozen=True, slots=True)
class Artwork:
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ArtworkFetchResult:
    artwork: Artwork
    source_url: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class _ArtworkCacheIdentity:
    key: str
    source_url: str | None
    release_mbid: str | None = None
    release_group_mbid: str | None = None


_CAA_CACHE_PATH = re.compile(
    r"^/(release|release-group)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/front-1200$",
    re.I,
)
_CACHE_RELATIVE_PATH = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.(?:jpg|png)$")


def _cache_identity(url: str) -> _ArtworkCacheIdentity:
    digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
    except ValueError:
        return _ArtworkCacheIdentity(f"unsafe:{digest}", None)
    normalized = urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, parsed.query, "")
    )
    match = _CAA_CACHE_PATH.fullmatch(parsed.path)
    if host == "coverartarchive.org" and match:
        identifier = _canonical_mbid(match.group(2))
        if identifier is not None and match.group(1).casefold() == "release":
            return _ArtworkCacheIdentity(
                f"caa-release:{identifier}",
                normalized,
                release_mbid=identifier,
            )
        if identifier is not None:
            return _ArtworkCacheIdentity(
                f"caa-release-group:{identifier}",
                normalized,
                release_group_mbid=identifier,
            )
    if host in {"i.ytimg.com", "img.youtube.com"}:
        normalized_digest = hashlib.sha256(normalized.encode()).hexdigest()
        return _ArtworkCacheIdentity(
            f"youtube-thumbnail:{normalized_digest}",
            normalized,
        )
    return _ArtworkCacheIdentity(f"unsafe:{digest}", None)


def _get_or_create_cache_row(session: Session, identity: _ArtworkCacheIdentity) -> ArtworkCache:
    session.execute(
        sqlite_insert(ArtworkCache)
        .values(
            cache_key=identity.key,
            release_mbid=identity.release_mbid,
            release_group_mbid=identity.release_group_mbid,
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=[ArtworkCache.cache_key])
    )
    row = session.scalar(select(ArtworkCache).where(ArtworkCache.cache_key == identity.key))
    if row is None:
        raise ArtworkError("artwork cache row could not be created")
    row.release_mbid = identity.release_mbid
    row.release_group_mbid = identity.release_group_mbid
    return row


def _safe_cache_relative_path(value: str) -> PurePosixPath:
    if not _CACHE_RELATIVE_PATH.fullmatch(value):
        raise ArtworkError("artwork cache row has an unsafe relative path")
    return PurePosixPath(value)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _bounded_header(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:300] if cleaned else None


def _host_is_allowed(host: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        normalized = pattern.casefold()
        if normalized.startswith("."):
            if host.endswith(normalized) and host != normalized[1:]:
                return True
        elif host == normalized:
            return True
    return False


def validate_artwork_url(
    value: str,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ARTWORK_HOSTS,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        raise SourceValidationError("artwork URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SourceValidationError("artwork URL is malformed") from exc
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        raise SourceValidationError("artwork URL must be credential-free HTTPS")
    if port not in (None, 443) or parsed.hostname is None:
        raise SourceValidationError("artwork URL has an invalid host or port")
    host = parsed.hostname.casefold()
    if host.endswith(".") or not _host_is_allowed(host, allowed_hosts):
        raise SourceValidationError("artwork host is not allowlisted")
    resolve_global_addresses(host, 443, resolver=resolver)
    return value


def normalize_artwork(
    data: bytes,
    *,
    max_input_bytes: int = 12 * 1024 * 1024,
    max_output_bytes: int = 8 * 1024 * 1024,
    max_pixels: int = 40_000_000,
    max_dimension: int = 1200,
) -> Artwork:
    if not data or len(data) > max_input_bytes:
        raise ArtworkError("artwork input is empty or exceeds the size limit")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise ArtworkError("artwork dimensions exceed the pixel limit")
            probe.verify()
        with Image.open(io.BytesIO(data)) as opened_image:
            image = ImageOps.exif_transpose(opened_image)
            image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            output = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                mime_type = "image/png"
            else:
                image.convert("RGB").save(
                    output,
                    format="JPEG",
                    quality=90,
                    optimize=True,
                    progressive=True,
                )
                mime_type = "image/jpeg"
            encoded = output.getvalue()
            final_width, final_height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ArtworkError("artwork is not a valid supported image") from exc
    if len(encoded) > max_output_bytes:
        raise ArtworkError("normalized artwork exceeds the output size limit")
    return Artwork(
        data=encoded,
        mime_type=mime_type,
        width=final_width,
        height=final_height,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


class ArtworkFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = DEFAULT_ARTWORK_HOSTS,
        resolver: Resolver = socket.getaddrinfo,
        timeout_seconds: float = 15.0,
        max_download_bytes: int = 12 * 1024 * 1024,
        max_redirects: int = 4,
        client: httpx.Client | None = None,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.resolver = resolver
        self.max_download_bytes = max_download_bytes
        self.max_redirects = max_redirects
        self._owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            headers={"User-Agent": "MusicAgent/0.1 artwork-fetcher"},
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ArtworkFetcher:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(self, url: str) -> Artwork:
        return self.fetch_with_metadata(url).artwork

    def fetch_with_metadata(self, url: str) -> ArtworkFetchResult:
        current = validate_artwork_url(
            url, allowed_hosts=self.allowed_hosts, resolver=self.resolver
        )
        try:
            for redirect_count in range(self.max_redirects + 1):
                with self.client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= self.max_redirects:
                            raise ArtworkError("artwork redirect policy was exceeded")
                        current = validate_artwork_url(
                            urljoin(current, location),
                            allowed_hosts=self.allowed_hosts,
                            resolver=self.resolver,
                        )
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        if response.status_code == 404:
                            raise ArtworkNotFound("artwork was not found") from exc
                        raise ArtworkError(
                            f"artwork request failed with HTTP {response.status_code}"
                        ) from exc
                    declared = response.headers.get("content-length")
                    if (
                        declared
                        and declared.isdecimal()
                        and int(declared) > self.max_download_bytes
                    ):
                        raise ArtworkError("artwork response exceeds the size limit")
                    chunks: list[bytes] = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > self.max_download_bytes:
                            raise ArtworkError("artwork response exceeds the size limit")
                        chunks.append(chunk)
                    return ArtworkFetchResult(
                        artwork=normalize_artwork(
                            b"".join(chunks), max_input_bytes=self.max_download_bytes
                        ),
                        source_url=current,
                        etag=_bounded_header(response.headers.get("etag")),
                        last_modified=_bounded_header(response.headers.get("last-modified")),
                    )
        except httpx.HTTPError as exc:
            raise ArtworkError("artwork request failed") from exc
        raise ArtworkError("artwork redirect policy was exceeded")


class ArtworkCacheService:
    """Persistent normalized-art cache around the validated synchronous fetcher."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        root: Path,
        fetcher: ArtworkFetcher,
        *,
        positive_ttl: timedelta = timedelta(days=30),
        negative_ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self.factory = factory
        self.root = root
        self.fetcher = fetcher
        self.positive_ttl = positive_ttl
        self.negative_ttl = negative_ttl

    def fetch(self, url: str) -> Artwork:
        now = datetime.now(UTC)
        identity = _cache_identity(url)
        try:
            cached = self._read(identity.key, now)
        except ArtworkNegativeCache:
            raise
        except (ArtworkError, OSError):
            cached = None
        if cached is not None:
            return cached
        try:
            fetched = self.fetcher.fetch_with_metadata(url)
        except SourceValidationError as exc:
            if "could not be resolved" not in str(exc):
                self._store_negative(identity, status="unsafe", now=now)
            raise
        except ArtworkNotFound:
            self._store_negative(identity, status="not_found", now=now)
            raise
        relative = self._store_blob(fetched.artwork)
        self._store_positive(identity, fetched, relative, now=now)
        return fetched.artwork

    def _read(self, key: str, now: datetime) -> Artwork | None:
        with self.factory() as session:
            row = session.scalar(select(ArtworkCache).where(ArtworkCache.cache_key == key))
            if row is None or row.expires_at is None or _aware(row.expires_at) <= now:
                return None
            if row.status in {"not_found", "unsafe"}:
                raise ArtworkNegativeCache(f"cached artwork result: {row.status}")
            if (
                row.status != "ok"
                or not row.relative_path
                or not row.content_sha256
                or not row.mime_type
                or row.width is None
                or row.height is None
            ):
                return None
            try:
                relative = _safe_cache_relative_path(row.relative_path)
                path = self.root.resolve(strict=True).joinpath(*relative.parts)
                file_stat = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(file_stat.st_mode):
                    return None
                data = path.read_bytes()
            except OSError:
                return None
            digest = hashlib.sha256(data).hexdigest()
            if digest != row.content_sha256:
                return None
            return Artwork(data, row.mime_type, row.width, row.height, digest)

    def _store_blob(self, artwork: Artwork) -> str:
        extension = "jpg" if artwork.mime_type == "image/jpeg" else "png"
        relative = PurePosixPath(artwork.sha256[:2], f"{artwork.sha256}.{extension}")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ArtworkError("artwork cache root must be a real directory")
        root = self.root.resolve(strict=True)
        directory = root / relative.parts[0]
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise ArtworkError("artwork cache contains an unsafe directory")
        destination = directory / relative.name
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise ArtworkError("artwork cache blob path is unsafe")
            if hashlib.sha256(destination.read_bytes()).hexdigest() != artwork.sha256:
                raise ArtworkError("artwork cache blob hash does not match its name")
            return relative.as_posix()
        temp = directory / f".{relative.name}.partial-{uuid.uuid4().hex}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(artwork.data)
                stream.flush()
            os.fsync(descriptor)
            try:
                os.link(temp, destination, follow_symlinks=False)
            except FileExistsError as exc:
                if destination.is_symlink() or not destination.is_file():
                    raise ArtworkError("artwork cache blob race produced an unsafe path") from exc
            directory_fd = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(descriptor)
            temp.unlink(missing_ok=True)
        return relative.as_posix()

    def _store_positive(
        self,
        identity: _ArtworkCacheIdentity,
        fetched: ArtworkFetchResult,
        relative: str,
        *,
        now: datetime,
    ) -> None:
        with self.factory.begin() as session:
            row = _get_or_create_cache_row(session, identity)
            row.source_url = fetched.source_url[:2048]
            row.content_sha256 = fetched.artwork.sha256
            row.mime_type = fetched.artwork.mime_type
            row.width = fetched.artwork.width
            row.height = fetched.artwork.height
            row.relative_path = relative
            row.etag = fetched.etag
            row.last_modified = fetched.last_modified
            row.status = "ok"
            row.expires_at = now + self.positive_ttl

    def _store_negative(
        self,
        identity: _ArtworkCacheIdentity,
        *,
        status: str,
        now: datetime,
    ) -> None:
        with self.factory.begin() as session:
            row = _get_or_create_cache_row(session, identity)
            row.source_url = identity.source_url
            row.content_sha256 = None
            row.mime_type = None
            row.width = None
            row.height = None
            row.relative_path = None
            row.etag = None
            row.last_modified = None
            row.status = status
            row.expires_at = now + self.negative_ttl


def cover_art_archive_urls(
    release_mbid: object,
    release_group_mbid: object,
) -> tuple[str, ...]:
    """Build only server-controlled 1200px Cover Art Archive endpoints."""

    urls: list[str] = []
    release = _canonical_mbid(release_mbid)
    if release is not None:
        urls.append(f"https://coverartarchive.org/release/{release}/front-1200")
    release_group = _canonical_mbid(release_group_mbid)
    if release_group is not None:
        urls.append(f"https://coverartarchive.org/release-group/{release_group}/front-1200")
    return tuple(urls)


def youtube_thumbnail_url(metadata: object) -> str | None:
    """Extract one provider-returned thumbnail; fetch-time policy validates its host."""

    if not isinstance(metadata, dict):
        return None
    primary = metadata.get("thumbnail")
    if isinstance(primary, str) and primary:
        return primary
    thumbnails = metadata.get("thumbnails")
    if not isinstance(thumbnails, list):
        return None
    for item in reversed(thumbnails[-20:]):
        if isinstance(item, dict):
            url = item.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def _canonical_mbid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def artwork_as_jpeg(artwork: Artwork, *, max_output_bytes: int = 8 * 1024 * 1024) -> bytes:
    """Return real JPEG bytes suitable for a `cover.jpg` sidecar."""

    if artwork.mime_type == "image/jpeg" and artwork.data.startswith(b"\xff\xd8"):
        return artwork.data
    try:
        with Image.open(io.BytesIO(artwork.data)) as opened_image:
            image = ImageOps.exif_transpose(opened_image)
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                background.alpha_composite(rgba)
                rgb = background.convert("RGB")
            else:
                rgb = image.convert("RGB")
            output = io.BytesIO()
            rgb.save(
                output,
                format="JPEG",
                quality=90,
                optimize=True,
                progressive=True,
            )
            encoded = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ArtworkError("normalized artwork could not be converted to JPEG") from exc
    if not encoded.startswith(b"\xff\xd8") or len(encoded) > max_output_bytes:
        raise ArtworkError("JPEG sidecar exceeds the output size limit")
    return encoded
