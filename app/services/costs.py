from __future__ import annotations

from dataclasses import asdict, dataclass

from app.config import Settings
from app.repositories.usage import UsageValues


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    input_per_million_usd: float | None
    cached_input_per_million_usd: float | None
    cache_write_per_million_usd: float | None
    output_per_million_usd: float | None
    web_search_low_usd: float | None
    web_search_medium_usd: float | None
    web_search_high_usd: float | None

    @classmethod
    def from_settings(cls, settings: Settings) -> PricingSnapshot:
        return cls(
            input_per_million_usd=settings.price_input_per_million_usd,
            cached_input_per_million_usd=settings.price_cached_input_per_million_usd,
            cache_write_per_million_usd=settings.price_cache_write_per_million_usd,
            output_per_million_usd=settings.price_output_per_million_usd,
            web_search_low_usd=settings.price_web_search_low_usd,
            web_search_medium_usd=settings.price_web_search_medium_usd,
            web_search_high_usd=settings.price_web_search_high_usd,
        )

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


class CostCalculator:
    def __init__(self, pricing: PricingSnapshot) -> None:
        self.pricing = pricing

    @classmethod
    def from_settings(cls, settings: Settings) -> CostCalculator:
        return cls(PricingSnapshot.from_settings(settings))

    def estimate_microusd(self, usage: UsageValues) -> int | None:
        regular_input = max(
            0,
            usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens,
        )
        components: list[tuple[int, float | None]] = [
            (regular_input, self.pricing.input_per_million_usd),
            (usage.cached_input_tokens, self.pricing.cached_input_per_million_usd),
            (usage.cache_write_tokens, self.pricing.cache_write_per_million_usd),
            (usage.output_tokens, self.pricing.output_per_million_usd),
        ]
        total_microusd = 0.0
        for count, price_per_million in components:
            if count <= 0:
                continue
            if price_per_million is None:
                return None
            # USD / million tokens converts directly to micro-USD / token.
            total_microusd += count * price_per_million

        if usage.web_search_count:
            context = usage.web_search_context or "medium"
            search_price = {
                "low": self.pricing.web_search_low_usd,
                "medium": self.pricing.web_search_medium_usd,
                "high": self.pricing.web_search_high_usd,
            }.get(context)
            if search_price is None:
                return None
            total_microusd += usage.web_search_count * search_price * 1_000_000
        return round(total_microusd)
