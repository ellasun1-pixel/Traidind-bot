from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from src.config import settings, AssetConfig
from src.market_data.candle import Candle, PriceQuote
from src.market_data.providers import ProviderAdapter, KrakenAdapter, CoinbaseAdapter
from src.market_data.validation import validate_candles, ValidationResult

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    candles: list[Candle]
    validation: ValidationResult
    provider_used: str
    fallback_used: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class AssetHealth:
    last_kraken_fetch: datetime | None = None
    last_coinbase_fetch: datetime | None = None
    last_successful_fetch: datetime | None = None
    current_provider: str = ""
    candle_freshness_hours: float = 0.0
    validation_status: str = ""
    latest_error: str = ""
    valid_candle_count: int = 0


@dataclass
class AnalysisSafetyResult:
    safe: bool
    daily_df: pd.DataFrame | None
    current_price: float | None
    provider_used: str
    reason: str
    asset_health: AssetHealth


class MarketDataPipeline:
    def __init__(self):
        self._primary: ProviderAdapter = KrakenAdapter()
        self._fallback: ProviderAdapter = CoinbaseAdapter()
        self._health: dict[str, AssetHealth] = {}

    def get_health(self, symbol: str) -> AssetHealth:
        if symbol not in self._health:
            self._health[symbol] = AssetHealth()
        return self._health[symbol]

    async def fetch_validated_candles(
        self,
        asset: AssetConfig,
        timeframe: str = "1d",
        count: int | None = None,
    ) -> FetchResult:
        if count is None:
            count = settings.target_fetch_candles

        health = self.get_health(asset.symbol)
        errors: list[str] = []

        candles, validation, provider = await self._try_provider(
            self._primary, asset.symbol, asset.kraken_pair, timeframe, count, health
        )
        if validation.valid:
            health.current_provider = provider
            health.last_successful_fetch = datetime.now(timezone.utc)
            health.validation_status = "valid"
            health.valid_candle_count = validation.valid_candle_count
            if validation.newest_candle:
                age = (datetime.now(timezone.utc) - validation.newest_candle).total_seconds() / 3600
                health.candle_freshness_hours = round(age, 1)
            health.latest_error = ""
            return FetchResult(
                candles=candles, validation=validation,
                provider_used=provider, fallback_used=False, errors=[],
            )

        primary_errors = validation.errors
        errors.extend([f"[{self._primary.name}] {e}" for e in primary_errors])
        logger.warning(
            "Primary provider %s failed for %s: %s",
            self._primary.name, asset.symbol, primary_errors,
        )

        candles, validation, provider = await self._try_provider(
            self._fallback, asset.symbol, asset.coinbase_pair, timeframe, count, health
        )
        if validation.valid:
            health.current_provider = provider
            health.last_successful_fetch = datetime.now(timezone.utc)
            health.validation_status = "valid (fallback)"
            health.valid_candle_count = validation.valid_candle_count
            if validation.newest_candle:
                age = (datetime.now(timezone.utc) - validation.newest_candle).total_seconds() / 3600
                health.candle_freshness_hours = round(age, 1)
            health.latest_error = ""
            return FetchResult(
                candles=candles, validation=validation,
                provider_used=provider, fallback_used=True, errors=errors,
            )

        errors.extend([f"[{self._fallback.name}] {e}" for e in validation.errors])
        health.validation_status = "invalid"
        health.latest_error = "; ".join(errors)
        logger.error("Both providers failed for %s: %s", asset.symbol, errors)

        return FetchResult(
            candles=[], validation=validation,
            provider_used="none", fallback_used=True, errors=errors,
        )

    async def _try_provider(
        self,
        provider: ProviderAdapter,
        symbol: str,
        pair: str,
        timeframe: str,
        count: int,
        health: AssetHealth,
    ) -> tuple[list[Candle], ValidationResult, str]:
        empty_result = ValidationResult(
            valid=False, candle_count=0, valid_candle_count=0,
            invalid_candle_count=0, oldest_candle=None, newest_candle=None,
            errors=["Provider fetch failed"], warnings=[],
        )
        try:
            raw_candles = await provider.fetch_ohlcv(symbol, pair, timeframe, count)
            if provider.name == "kraken":
                health.last_kraken_fetch = datetime.now(timezone.utc)
            else:
                health.last_coinbase_fetch = datetime.now(timezone.utc)
        except Exception as e:
            logger.warning("%s fetch error for %s: %s", provider.name, symbol, e)
            empty_result.errors = [f"Fetch error: {type(e).__name__}: {e}"]
            return [], empty_result, provider.name

        valid_candles, validation = validate_candles(raw_candles)
        return valid_candles, validation, provider.name

    async def get_prices(
        self, asset: AssetConfig
    ) -> tuple[Optional[PriceQuote], Optional[PriceQuote]]:
        kraken_quote = None
        coinbase_quote = None

        try:
            kraken_quote = await self._primary.get_current_price(
                asset.symbol, asset.kraken_pair
            )
        except Exception as e:
            logger.warning("Kraken price failed for %s: %s", asset.symbol, e)

        try:
            coinbase_quote = await self._fallback.get_current_price(
                asset.symbol, asset.coinbase_pair
            )
        except Exception as e:
            logger.warning("Coinbase price failed for %s: %s", asset.symbol, e)

        return kraken_quote, coinbase_quote

    async def check_divergence(
        self,
        asset: AssetConfig,
        threshold_pct: float | None = None,
    ) -> tuple[bool, float, Optional[PriceQuote]]:
        if threshold_pct is None:
            threshold_pct = settings.max_provider_price_divergence_pct

        kraken_quote, coinbase_quote = await self.get_prices(asset)

        if kraken_quote is None and coinbase_quote is None:
            return False, 0.0, None

        if kraken_quote is None:
            return False, 0.0, coinbase_quote
        if coinbase_quote is None:
            return False, 0.0, kraken_quote

        mid = (kraken_quote.price + coinbase_quote.price) / 2
        if mid == 0:
            return False, 0.0, kraken_quote

        divergence = abs(kraken_quote.price - coinbase_quote.price) / mid
        is_divergent = divergence > threshold_pct

        return is_divergent, divergence, kraken_quote

    async def get_analysis_ready_data(
        self, asset: AssetConfig
    ) -> AnalysisSafetyResult:
        health = self.get_health(asset.symbol)

        fetch_result = await self.fetch_validated_candles(asset, "1d")

        if not fetch_result.validation.valid:
            reason = f"DATA_INVALID: {'; '.join(fetch_result.validation.errors)}"
            health.validation_status = "invalid"
            health.latest_error = reason
            return AnalysisSafetyResult(
                safe=False, daily_df=None, current_price=None,
                provider_used=fetch_result.provider_used,
                reason=reason, asset_health=health,
            )

        is_divergent, div_pct, best_quote = await self.check_divergence(asset)

        if is_divergent:
            reason = (
                f"DATA_INVALID: Provider price divergence {div_pct:.2%} "
                f"exceeds threshold {settings.max_provider_price_divergence_pct:.2%}"
            )
            health.validation_status = "divergent"
            health.latest_error = reason
            return AnalysisSafetyResult(
                safe=False, daily_df=None, current_price=None,
                provider_used=fetch_result.provider_used,
                reason=reason, asset_health=health,
            )

        if best_quote is not None:
            current_price = best_quote.price
        else:
            reason = "DATA_UNAVAILABLE: No current price from any provider"
            health.validation_status = "no_price"
            health.latest_error = reason
            return AnalysisSafetyResult(
                safe=False, daily_df=None, current_price=None,
                provider_used=fetch_result.provider_used,
                reason=reason, asset_health=health,
            )

        if current_price <= 0:
            reason = f"DATA_INVALID: Current price {current_price} <= 0"
            return AnalysisSafetyResult(
                safe=False, daily_df=None, current_price=None,
                provider_used=fetch_result.provider_used,
                reason=reason, asset_health=health,
            )

        daily_df = _candles_to_dataframe(fetch_result.candles)

        health.validation_status = "ready"
        health.latest_error = ""
        return AnalysisSafetyResult(
            safe=True, daily_df=daily_df, current_price=current_price,
            provider_used=fetch_result.provider_used,
            reason="", asset_health=health,
        )


def _candles_to_dataframe(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()

    rows = [
        {
            "open_time": c.open_time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    df = pd.DataFrame(rows)
    df.sort_values("open_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
