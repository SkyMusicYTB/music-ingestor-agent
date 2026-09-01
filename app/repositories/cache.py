from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import ExternalCache


@dataclass(frozen=True, slots=True)
class CacheEntry:
    payload: Any
    expires_at: datetime
    etag: str | None
    last_modified: str | None


class ExternalCacheRepository:
    """Small JSON cache backed by the application's SQLite database.

    Transactions are deliberately owned by the caller. This keeps cache writes
    composable with request accounting and avoids hidden commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(
        self,
        namespace: str,
        cache_key: str,
        *,
        now: datetime | None = None,
    ) -> CacheEntry | None:
        namespace = _validate_namespace(namespace)
        cache_key = _bounded_key(cache_key)
        current = now or datetime.now(UTC)
        row = self._session.scalar(
            select(ExternalCache).where(
                ExternalCache.namespace == namespace,
                ExternalCache.cache_key == cache_key,
            )
        )
        if row is None:
            return None
        if _as_utc(row.expires_at) <= _as_utc(current):
            self._session.delete(row)
            return None
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, json.JSONDecodeError):
            self._session.delete(row)
            return None
        row.last_accessed_at = current
        return CacheEntry(
            payload=payload,
            expires_at=row.expires_at,
            etag=row.etag,
            last_modified=row.last_modified,
        )

    def put(
        self,
        namespace: str,
        cache_key: str,
        payload: Any,
        *,
        ttl: timedelta,
        etag: str | None = None,
        last_modified: str | None = None,
        now: datetime | None = None,
    ) -> ExternalCache:
        if ttl.total_seconds() <= 0:
            raise ValueError("cache TTL must be positive")
        namespace = _validate_namespace(namespace)
        cache_key = _bounded_key(cache_key)
        current = now or datetime.now(UTC)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        row = self._session.scalar(
            select(ExternalCache).where(
                ExternalCache.namespace == namespace,
                ExternalCache.cache_key == cache_key,
            )
        )
        if row is None:
            row = ExternalCache(namespace=namespace, cache_key=cache_key, payload_json=encoded)
            self._session.add(row)
        row.payload_json = encoded
        row.expires_at = current + ttl
        row.etag = etag
        row.last_modified = last_modified
        row.last_accessed_at = current
        return row

    def delete_expired(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        result = self._session.execute(
            delete(ExternalCache).where(ExternalCache.expires_at <= current)
        )
        return int(result.rowcount or 0)


def _validate_namespace(namespace: str) -> str:
    value = namespace.strip()
    if not value or len(value) > 80:
        raise ValueError("cache namespace must contain 1-80 characters")
    return value


def _bounded_key(cache_key: str) -> str:
    value = cache_key.strip()
    if not value:
        raise ValueError("cache key cannot be empty")
    if len(value) <= 500:
        return value
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
