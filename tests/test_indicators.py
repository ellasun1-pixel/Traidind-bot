"""
Indicator validation tests — verifies EMA, efficiency ratio, ADX, realized
volatility, warm-up tracking, and DATA_INSUFFICIENT regime classification.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.indicators import (
    ema, efficiency_ratio, adx, realized_volatility,
    compute_indicators, indicator_warmup_status, WARMUP,
)
from src.strategy.regime import (
    classify_regime, MarketRegime, regime_nan_fields,
)


def _make_close_series(n: int = 300, seed: int = 42) -> pd.Series:
    rng = np.random.RandomState(seed)
    returns = rng.normal(0.0005, 0.02, n)
    prices = 100 * np.cumprod(1 + returns)
    return pd.Series(prices, name="close")


def _make_ohlcv_df(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    returns = rng.normal(0.0005, 0.02, n)
    closes = 100 * np.cumprod(1 + returns)
    highs = closes * (1 + rng.uniform(0, 0.02, n))
    lows = closes * (1 - rng.uniform(0, 0.02, n))
    opens = closes * (1 + rng.normal(0, 0.005, n))
    dates = pd.date_range("2025-07-01", periods=n, freq="1D")
    return pd.DataFrame({
        "open_time": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": rng.uniform(100, 1000, n),
    })


class TestEMAVerification:
    def test_ema_matches_pandas_ewm(self):
        close = _make_close_series()
        for period in [50, 200]:
            our_ema = ema(close, period)
            ref_ema = close.ewm(span=period, adjust=False).mean()
            pd.testing.assert_series_equal(our_ema, ref_ema, check_names=False)

    def test_ema_known_values(self):
        prices = pd.Series([10.0, 11.0, 12.0, 11.5, 13.0])
        result = ema(prices, 3)
        alpha = 2 / (3 + 1)
        expected = [10.0]
        for p in [11.0, 12.0, 11.5, 13.0]:
            expected.append(alpha * p + (1 - alpha) * expected[-1])
        np.testing.assert_allclose(result.values, expected, atol=1e-10)

    def test_ema50_ema200_populated_at_300_candles(self):
        df = _make_ohlcv_df(300)
        enriched = compute_indicators(df)
        assert not np.isnan(enriched.iloc[-1]["ema50"])
        assert not np.isnan(enriched.iloc[-1]["ema200"])

    def test_ema_converges_on_constant(self):
        prices = pd.Series([50.0] * 100)
        result = ema(prices, 20)
        np.testing.assert_allclose(result.values, 50.0, atol=1e-10)


class TestEfficiencyRatio:
    def test_er_perfect_trend(self):
        prices = pd.Series(np.linspace(100, 120, 50))
        er = efficiency_ratio(prices, 20)
        assert er.iloc[-1] == pytest.approx(1.0, abs=0.01)

    def test_er_random_walk_low(self):
        rng = np.random.RandomState(99)
        prices = pd.Series(100 + rng.normal(0, 1, 200).cumsum())
        er = efficiency_ratio(prices, 20)
        assert er.iloc[-1] < 0.5


class TestRealizedVolatility:
    def test_rvol_not_nan_after_warmup(self):
        close = _make_close_series(100)
        rv = realized_volatility(close, 20)
        assert not np.isnan(rv.iloc[-1])
        assert np.isnan(rv.iloc[15])

    def test_rvol_annualized(self):
        close = _make_close_series(100)
        rv = realized_volatility(close, 20)
        assert rv.iloc[-1] > 0
        assert rv.iloc[-1] < 5.0


class TestRVolMedianFix:
    def test_rvol_median_not_nan_with_300_candles(self):
        df = _make_ohlcv_df(300)
        enriched = compute_indicators(df)
        latest = enriched.iloc[-1]
        assert not np.isnan(latest["rvol_median_252"]), "rvol_median_252 should not be NaN with 300 candles"
        assert not np.isnan(latest["rvol_pct25"]), "rvol_pct25 should not be NaN with 300 candles"

    def test_rvol_median_is_nan_with_too_few_candles(self):
        df = _make_ohlcv_df(50)
        enriched = compute_indicators(df)
        latest = enriched.iloc[-1]
        assert np.isnan(latest["rvol_median_252"])

    def test_panic_can_trigger_with_valid_rvol(self):
        df = _make_ohlcv_df(300)
        enriched = compute_indicators(df)
        row = enriched.iloc[-1].copy()
        row["price_change_48h"] = -0.15
        rvol_med = row["rvol_median_252"]
        row["rvol"] = rvol_med * 2.0
        regime = classify_regime(row)
        assert regime == MarketRegime.PANIC

    def test_lowvol_can_trigger_with_valid_rvol(self):
        df = _make_ohlcv_df(300)
        enriched = compute_indicators(df)
        row = enriched.iloc[-1].copy()
        row["price_change_48h"] = 0.01
        row["er20"] = 0.1
        pct25 = row["rvol_pct25"]
        row["rvol"] = pct25 * 0.5
        regime = classify_regime(row)
        assert regime == MarketRegime.LOWVOL


class TestDataInsufficient:
    def test_nan_er20_triggers_data_insufficient(self):
        row = pd.Series({
            "close": 100, "ema50": 99, "ema200": 98,
            "er20": np.nan, "rvol": 0.3, "rvol_median_252": 0.25,
            "rvol_pct25": 0.2, "price_change_48h": 0.01,
        })
        assert classify_regime(row) == MarketRegime.DATA_INSUFFICIENT

    def test_nan_ema200_triggers_data_insufficient(self):
        row = pd.Series({
            "close": 100, "ema50": 99, "ema200": np.nan,
            "er20": 0.4, "rvol": 0.3, "rvol_median_252": 0.25,
            "rvol_pct25": 0.2, "price_change_48h": 0.01,
        })
        assert classify_regime(row) == MarketRegime.DATA_INSUFFICIENT

    def test_regime_nan_fields_identifies_nans(self):
        row = pd.Series({
            "close": 100, "ema50": np.nan, "ema200": 98,
            "er20": 0.4, "rvol": np.nan, "rvol_median_252": 0.25,
            "rvol_pct25": 0.2, "price_change_48h": 0.01,
        })
        nans = regime_nan_fields(row)
        assert "ema50" in nans
        assert "rvol" in nans
        assert "er20" not in nans

    def test_all_valid_not_data_insufficient(self):
        row = pd.Series({
            "close": 100, "ema50": 99, "ema200": 98,
            "er20": 0.2, "rvol": 0.3, "rvol_median_252": 0.25,
            "rvol_pct25": 0.2, "price_change_48h": 0.01,
        })
        assert classify_regime(row) != MarketRegime.DATA_INSUFFICIENT


class TestWarmupStatus:
    def test_300_candles_all_warm(self):
        status = indicator_warmup_status(300)
        assert all(status.values())

    def test_50_candles_partial(self):
        status = indicator_warmup_status(50)
        assert status["ema50"] is True
        assert status["ema200"] is False
        assert status["rvol_median_60"] is False

    def test_warmup_dict_complete(self):
        df = _make_ohlcv_df(300)
        enriched = compute_indicators(df)
        for col in ["ema50", "ema200", "er20", "rvol", "rvol_median_252", "rvol_pct25"]:
            assert col in enriched.columns or col.replace("_252", "_60") in enriched.columns
