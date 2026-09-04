from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ExternalCache, OpenAICall
from app.repositories.cache import ExternalCacheRepository
from app.repositories.usage import OpenAIUsageRepository, UsageValues
from app.services.costs import CostCalculator, PricingSnapshot
from app.workers.source_discovery_state import (
    SOURCE_SEARCH_CACHE_NAMESPACE,
    delete_expired_source_search_cache,
)


def database() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_external_cache_expires_and_replaces_entries() -> None:
    factory = database()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with factory.begin() as session:
        cache = ExternalCacheRepository(session)
        cache.put("musicbrainz", "artist:key", {"value": 1}, ttl=timedelta(hours=1), now=now)
        cache.put("musicbrainz", "artist:key", {"value": 2}, ttl=timedelta(hours=2), now=now)
    with factory.begin() as session:
        entry = ExternalCacheRepository(session).get("musicbrainz", "artist:key", now=now)
        assert entry is not None
        assert entry.payload == {"value": 2}
    with factory.begin() as session:
        assert (
            ExternalCacheRepository(session).get(
                "musicbrainz", "artist:key", now=now + timedelta(hours=3)
            )
            is None
        )


def test_expired_source_search_cleanup_is_namespace_scoped() -> None:
    factory = database()
    now = datetime(2026, 9, 1, tzinfo=UTC)
    with factory.begin() as session:
        cache = ExternalCacheRepository(session)
        cache.put(
            SOURCE_SEARCH_CACHE_NAMESPACE,
            "expired-search",
            {"value": 1},
            ttl=timedelta(hours=1),
            now=now,
        )
        cache.put(
            SOURCE_SEARCH_CACHE_NAMESPACE,
            "live-search",
            {"value": 2},
            ttl=timedelta(hours=3),
            now=now,
        )
        cache.put(
            "musicbrainz",
            "expired-shared-provider-entry",
            {"value": 3},
            ttl=timedelta(hours=1),
            now=now,
        )

    assert delete_expired_source_search_cache(factory, now=now + timedelta(hours=2)) == 1
    with factory() as session:
        remaining = set(session.execute(select(ExternalCache.namespace, ExternalCache.cache_key)))
    assert remaining == {
        (SOURCE_SEARCH_CACHE_NAMESPACE, "live-search"),
        ("musicbrainz", "expired-shared-provider-entry"),
    }


def test_usage_accounting_and_cost_snapshot() -> None:
    factory = database()
    pricing = PricingSnapshot(
        input_per_million_usd=2.0,
        cached_input_per_million_usd=0.5,
        cache_write_per_million_usd=3.0,
        output_per_million_usd=8.0,
        web_search_low_usd=0.01,
        web_search_medium_usd=0.02,
        web_search_high_usd=0.03,
    )
    usage = UsageValues(
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=50,
        total_tokens=150,
        web_search_count=1,
        web_search_context="low",
    )
    cost = CostCalculator(pricing).estimate_microusd(usage)
    assert cost == 10_570

    with factory.begin() as session:
        repository = OpenAIUsageRepository(session)
        row = repository.start_call(
            request_id=None,
            model="test-model",
            prompt_version="v1",
            prompt_hash="0" * 64,
            pricing_snapshot=pricing.as_dict(),
        )
        repository.complete_call(
            row,
            response_id="resp_1",
            provider_request_id="req_1",
            usage=usage,
            latency_ms=12,
            service_tier="default",
            estimated_cost_microusd=cost,
        )
        identifier = row.id
    with factory() as session:
        stored = session.get(OpenAICall, identifier)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.estimated_cost_microusd == 10_570
        assert stored.input_tokens == 100
