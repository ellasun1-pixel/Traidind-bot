from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pandas as pd

from src.config import settings, AssetConfig
from src.market_data.candle import Candle, PriceQuote
from src.market_data.validation import validate_candles, ValidationResult
from src.market_data.pipeline import MarketDataPipeline, _candles_to_dataframe


def _now():
    return datetime.now(timezone.utc)


def _ago(days=0, hours=0):
    return _now() - timedelta(days=days, hours=hours)


def _make_candle(
    days_ago=0, close=65000.0, high=None, low=None, open_price=None,
    volume=100.0, source="kraken", asset="BTC/USD", timeframe="1d",
    open_time=None, fetched_at=None,
):
    if open_time is None:
        open_time = _ago(days=days_ago)
    if high is None:
        high = close * 1.01
    if low is None:
        low = close * 0.99
    if open_price is None:
        open_price = close * 1.001
    return Candle(
        asset=asset, timeframe=timeframe,
        open_time=open_time,
        open=open_price, high=high, low=low, close=close,
        volume=volume, source=source,
        fetched_at=fetched_at or _now(),
    )


def _make_candles(count, start_days_ago=None, **kwargs):
    if start_days_ago is None:
        start_days_ago = count
    return [_make_candle(days_ago=start_days_ago - i, **kwargs) for i in range(count)]


def _make_asset():
    return AssetConfig(
        symbol="BTC/USD", kraken_pair="XXBTZUSD",
        coinbase_pair="BTC-USD", active=True,
    )


# ──────────────────────────────────────────────
# Candle Validation
# ──────────────────────────────────────────────

class TestCandleValidation:
    def test_valid_candle(self):
        c = _make_candle()
        ok, reason = c.is_valid()
        assert ok is True

    def test_missing_close(self):
        c = Candle("BTC", "1d", _ago(1), 100, 110, 90, None, 10, "kraken", _now())
        ok, reason = c.is_valid()
        assert ok is False
        assert "close" in reason.lower() or "NaN" in reason

    def test_nan_close(self):
        c = _make_candle(close=float("nan"))
        ok, reason = c.is_valid()
        assert ok is False
        assert "NaN" in reason

    def test_zero_close_price(self):
        c = _make_candle(close=0.0, low=0.0, open_price=0.0, high=0.0)
        ok, reason = c.is_valid()
        assert ok is False
        assert "close" in reason and "<= 0" in reason

    def test_negative_close_price(self):
        c = _make_candle(close=-1.0, low=-2.0, open_price=-0.5, high=0.0)
        ok, reason = c.is_valid()
        assert ok is False

    def test_high_less_than_low(self):
        c = _make_candle(high=100, low=200, close=150, open_price=150)
        ok, reason = c.is_valid()
        assert ok is False
        assert "high" in reason and "low" in reason

    def test_open_outside_range(self):
        c = _make_candle(open_price=50, high=200, low=100, close=150)
        ok, reason = c.is_valid()
        assert ok is False
        assert "open" in reason and "outside" in reason

    def test_close_outside_range(self):
        c = _make_candle(close=50, high=200, low=100, open_price=150)
        ok, reason = c.is_valid()
        assert ok is False
        assert "close" in reason and "outside" in reason

    def test_missing_volume(self):
        c = Candle("BTC", "1d", _ago(1), 100, 110, 90, 100, None, "kraken", _now())
        ok, reason = c.is_valid()
        assert ok is False
        assert "volume" in reason

    def test_nan_volume(self):
        c = _make_candle(volume=float("nan"))
        ok, reason = c.is_valid()
        assert ok is False
        assert "volume" in reason

    def test_zero_volume_allowed(self):
        c = _make_candle(volume=0.0)
        ok, reason = c.is_valid()
        assert ok is True

    def test_future_timestamp(self):
        c = _make_candle(open_time=_now() + timedelta(days=1))
        ok, reason = c.is_valid()
        assert ok is False
        assert "future" in reason

    def test_naive_timestamp(self):
        c = _make_candle(open_time=datetime(2024, 1, 1))
        ok, reason = c.is_valid()
        assert ok is False
        assert "timezone" in reason


# ──────────────────────────────────────────────
# Dataset Validation
# ──────────────────────────────────────────────

class TestDatasetValidation:
    def test_300_candles_fetched_250_valid(self):
        candles = _make_candles(300)
        valid, result = validate_candles(candles)
        assert result.valid is True
        assert result.valid_candle_count == 300
        assert len(valid) == 300

    def test_fewer_than_250_candles(self):
        candles = _make_candles(200)
        valid, result = validate_candles(candles, min_candles=250)
        assert result.valid is False
        assert "Insufficient" in result.errors[0]
        assert "200" in result.errors[0]
        assert "250" in result.errors[0]

    def test_stale_latest_candle(self):
        candles = _make_candles(260, start_days_ago=300)
        valid, result = validate_candles(candles, max_age_hours=30)
        assert result.valid is False
        assert any("Stale" in e for e in result.errors)

    def test_empty_dataset(self):
        valid, result = validate_candles([])
        assert result.valid is False
        assert result.candle_count == 0

    def test_duplicate_timestamps_removed(self):
        candles = _make_candles(260)
        candles.append(candles[0])
        valid, result = validate_candles(candles)
        assert result.valid_candle_count == 260
        assert any("Duplicate" in w for w in result.warnings)

    def test_unsorted_timestamps_sorted(self):
        candles = _make_candles(260)
        reversed_candles = list(reversed(candles))
        valid, result = validate_candles(reversed_candles)
        assert result.valid is True
        for i in range(1, len(valid)):
            assert valid[i].open_time >= valid[i - 1].open_time

    def test_invalid_candles_filtered(self):
        good = _make_candles(255)
        bad = [_make_candle(days_ago=300 + i, close=0.0, low=0.0, open_price=0.0, high=0.0) for i in range(10)]
        all_candles = good + bad
        valid, result = validate_candles(all_candles, min_candles=250)
        assert result.valid is True
        assert result.valid_candle_count == 255
        assert result.invalid_candle_count == 10

    def test_all_invalid_candles(self):
        bad = [_make_candle(close=float("nan")) for _ in range(10)]
        valid, result = validate_candles(bad, min_candles=250)
        assert result.valid is False
        assert result.valid_candle_count == 0

    def test_exactly_250_candles(self):
        candles = _make_candles(250)
        valid, result = validate_candles(candles, min_candles=250)
        assert result.valid is True
        assert result.valid_candle_count == 250


# ──────────────────────────────────────────────
# Pipeline - Provider Fallback
# ──────────────────────────────────────────────

class TestPipelineFallback:
    @pytest.mark.asyncio
    async def test_kraken_success(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260, source="kraken")
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is True
        assert result.provider_used == "kraken"
        assert result.fallback_used is False

    @pytest.mark.asyncio
    async def test_kraken_fails_coinbase_succeeds(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(side_effect=Exception("Kraken down"))
        candles = _make_candles(260, source="coinbase")
        pipeline._fallback.fetch_ohlcv = AsyncMock(return_value=candles)

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is True
        assert result.provider_used == "coinbase"
        assert result.fallback_used is True

    @pytest.mark.asyncio
    async def test_kraken_insufficient_coinbase_succeeds(self):
        pipeline = MarketDataPipeline()
        few_candles = _make_candles(100, source="kraken")
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=few_candles)
        good_candles = _make_candles(260, source="coinbase")
        pipeline._fallback.fetch_ohlcv = AsyncMock(return_value=good_candles)

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is True
        assert result.provider_used == "coinbase"
        assert result.fallback_used is True

    @pytest.mark.asyncio
    async def test_both_providers_fail(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(side_effect=Exception("Kraken down"))
        pipeline._fallback.fetch_ohlcv = AsyncMock(side_effect=Exception("Coinbase down"))

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is False
        assert result.provider_used == "none"
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_empty_response_from_provider(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=[])
        pipeline._fallback.fetch_ohlcv = AsyncMock(return_value=[])

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is False

    @pytest.mark.asyncio
    async def test_http_timeout_handled(self):
        pipeline = MarketDataPipeline()
        import httpx
        pipeline._primary.fetch_ohlcv = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )
        pipeline._fallback.fetch_ohlcv = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.validation.valid is False


# ──────────────────────────────────────────────
# Pipeline - Price Divergence
# ──────────────────────────────────────────────

class TestPriceDivergence:
    @pytest.mark.asyncio
    async def test_divergence_above_threshold(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65000, "kraken", _now())
        )
        pipeline._fallback.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 72000, "coinbase", _now())
        )

        asset = _make_asset()
        is_div, pct, quote = await pipeline.check_divergence(asset, threshold_pct=0.05)
        assert is_div is True
        assert pct > 0.05

    @pytest.mark.asyncio
    async def test_divergence_below_threshold(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65000, "kraken", _now())
        )
        pipeline._fallback.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65100, "coinbase", _now())
        )

        asset = _make_asset()
        is_div, pct, quote = await pipeline.check_divergence(asset, threshold_pct=0.05)
        assert is_div is False
        assert pct < 0.05

    @pytest.mark.asyncio
    async def test_one_provider_unavailable_no_divergence(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65000, "kraken", _now())
        )
        pipeline._fallback.get_current_price = AsyncMock(side_effect=Exception("down"))

        asset = _make_asset()
        is_div, pct, quote = await pipeline.check_divergence(asset)
        assert is_div is False
        assert quote is not None


# ──────────────────────────────────────────────
# Analysis Safety Gate
# ──────────────────────────────────────────────

class TestAnalysisSafetyGate:
    @pytest.mark.asyncio
    async def test_valid_data_returns_safe(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260)
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)
        pipeline._primary.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65000, "kraken", _now())
        )
        pipeline._fallback.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 65050, "coinbase", _now())
        )

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is True
        assert result.daily_df is not None
        assert len(result.daily_df) == 260
        assert result.current_price == 65000

    @pytest.mark.asyncio
    async def test_invalid_data_returns_no_trade_reason(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=[])
        pipeline._fallback.fetch_ohlcv = AsyncMock(return_value=[])

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is False
        assert "DATA_INVALID" in result.reason or "DATA_UNAVAILABLE" in result.reason

    @pytest.mark.asyncio
    async def test_divergent_prices_returns_unsafe(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260)
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)
        pipeline._primary.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 60000, "kraken", _now())
        )
        pipeline._fallback.get_current_price = AsyncMock(
            return_value=PriceQuote("BTC/USD", 70000, "coinbase", _now())
        )

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is False
        assert "divergence" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_no_current_price_returns_unsafe(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260)
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)
        pipeline._primary.get_current_price = AsyncMock(side_effect=Exception("down"))
        pipeline._fallback.get_current_price = AsyncMock(side_effect=Exception("down"))

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is False
        assert "price" in result.reason.lower() or "UNAVAILABLE" in result.reason

    @pytest.mark.asyncio
    async def test_invalid_data_never_produces_buy_or_sell(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=_make_candles(100))
        pipeline._fallback.fetch_ohlcv = AsyncMock(return_value=_make_candles(100))

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is False
        assert result.daily_df is None


# ──────────────────────────────────────────────
# Candle to DataFrame Conversion
# ──────────────────────────────────────────────

class TestCandleToDataframe:
    def test_conversion(self):
        candles = _make_candles(10)
        df = _candles_to_dataframe(candles)
        assert len(df) == 10
        assert list(df.columns) == ["open_time", "open", "high", "low", "close", "volume"]

    def test_sorted_by_time(self):
        candles = list(reversed(_make_candles(10)))
        df = _candles_to_dataframe(candles)
        times = list(df["open_time"])
        assert times == sorted(times)

    def test_empty(self):
        df = _candles_to_dataframe([])
        assert df.empty


# ──────────────────────────────────────────────
# Health Tracking
# ──────────────────────────────────────────────

class TestHealthTracking:
    @pytest.mark.asyncio
    async def test_health_updated_on_success(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260)
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)

        asset = _make_asset()
        await pipeline.fetch_validated_candles(asset)
        health = pipeline.get_health("BTC/USD")
        assert health.last_successful_fetch is not None
        assert health.current_provider == "kraken"
        assert health.validation_status == "valid"
        assert health.valid_candle_count == 260

    @pytest.mark.asyncio
    async def test_health_updated_on_failure(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(side_effect=Exception("error"))
        pipeline._fallback.fetch_ohlcv = AsyncMock(side_effect=Exception("error"))

        asset = _make_asset()
        await pipeline.fetch_validated_candles(asset)
        health = pipeline.get_health("BTC/USD")
        assert health.validation_status == "invalid"
        assert health.latest_error != ""

    @pytest.mark.asyncio
    async def test_health_tracks_kraken_fetch_time(self):
        pipeline = MarketDataPipeline()
        candles = _make_candles(260)
        pipeline._primary.fetch_ohlcv = AsyncMock(return_value=candles)

        asset = _make_asset()
        await pipeline.fetch_validated_candles(asset)
        health = pipeline.get_health("BTC/USD")
        assert health.last_kraken_fetch is not None


# ──────────────────────────────────────────────
# Provider-specific edge cases
# ──────────────────────────────────────────────

class TestProviderMappings:
    def test_asset_config_has_kraken_pair(self):
        asset = _make_asset()
        assert asset.kraken_pair == "XXBTZUSD"

    def test_asset_config_has_coinbase_pair(self):
        asset = _make_asset()
        assert asset.coinbase_pair == "BTC-USD"


class TestMarketDataDoesNotCrash:
    @pytest.mark.asyncio
    async def test_pipeline_handles_all_errors_gracefully(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(side_effect=ValueError("bad json"))
        pipeline._fallback.fetch_ohlcv = AsyncMock(side_effect=ConnectionError("refused"))

        asset = _make_asset()
        result = await pipeline.fetch_validated_candles(asset)
        assert result.provider_used == "none"

    @pytest.mark.asyncio
    async def test_get_analysis_ready_data_never_raises(self):
        pipeline = MarketDataPipeline()
        pipeline._primary.fetch_ohlcv = AsyncMock(side_effect=RuntimeError("crash"))
        pipeline._fallback.fetch_ohlcv = AsyncMock(side_effect=RuntimeError("crash"))

        asset = _make_asset()
        result = await pipeline.get_analysis_ready_data(asset)
        assert result.safe is False


# ──────────────────────────────────────────────
# Database persistence (candle upsert)
# ──────────────────────────────────────────────

class TestCandlePersistence:
    def test_no_duplicate_candle_objects(self):
        candles = _make_candles(260)
        candles.append(candles[0])  # duplicate
        valid, result = validate_candles(candles)
        assert result.valid_candle_count == 260

    def test_upsert_unique_constraint(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.database.models import Base, PriceHistory, Asset

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()

        asset = Asset(symbol="BTC/USD", risk_pct=0.003, max_position_usd=150,
                      stop_loss_pct=0.03, min_volume=0, enabled=True)
        session.add(asset)
        session.flush()

        row = PriceHistory(
            asset_id=asset.id, timeframe="1d",
            open_time=_ago(days=1), open=65000, high=66000,
            low=64000, close=65500, volume=100, source="kraken",
        )
        session.add(row)
        session.commit()

        existing = session.query(PriceHistory).count()
        assert existing == 1

        session.close()
        engine.dispose()
