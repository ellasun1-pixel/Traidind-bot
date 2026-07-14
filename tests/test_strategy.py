import pytest
import numpy as np
import pandas as pd

from src.strategy.indicators import compute_indicators, ema, efficiency_ratio, adx
from src.strategy.regime import classify_regime, MarketRegime
from src.strategy.engine import StrategyEngine


def _make_daily_df(n=300, base_price=50000, trend=0.001):
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    prices = [base_price]
    for i in range(1, n):
        change = np.random.normal(trend, 0.02)
        prices.append(prices[-1] * (1 + change))
    df = pd.DataFrame({
        "open_time": dates,
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1000.0] * n,
    })
    return df


class TestRegimeClassification:
    def test_panic_regime(self):
        row = pd.Series({
            "price_change_48h": -0.12,
            "rvol": 0.9,
            "rvol_median_252": 0.4,
            "rvol_pct25": 0.3,
            "er20": 0.5,
            "close": 45000,
            "ema200": 50000,
            "ema50": 48000,
        })
        assert classify_regime(row) == MarketRegime.PANIC

    def test_lowvol_regime(self):
        row = pd.Series({
            "price_change_48h": 0.001,
            "rvol": 0.1,
            "rvol_median_252": 0.5,
            "rvol_pct25": 0.15,
            "er20": 0.2,
            "close": 50000,
            "ema200": 49000,
            "ema50": 50500,
        })
        assert classify_regime(row) == MarketRegime.LOWVOL

    def test_trend_regime(self):
        row = pd.Series({
            "price_change_48h": 0.02,
            "rvol": 0.4,
            "rvol_median_252": 0.4,
            "rvol_pct25": 0.3,
            "er20": 0.45,
            "close": 52000,
            "ema200": 48000,
            "ema50": 50000,
        })
        assert classify_regime(row) == MarketRegime.TREND

    def test_chop_default(self):
        row = pd.Series({
            "price_change_48h": -0.01,
            "rvol": 0.4,
            "rvol_median_252": 0.4,
            "rvol_pct25": 0.3,
            "er20": 0.25,
            "close": 50000,
            "ema200": 51000,
            "ema50": 49000,
        })
        assert classify_regime(row) == MarketRegime.CHOP


class TestIndicators:
    def test_ema_calculation(self):
        s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        result = ema(s, 3)
        assert len(result) == 10
        assert result.iloc[-1] > result.iloc[0]

    def test_efficiency_ratio_range(self):
        np.random.seed(42)
        s = pd.Series(np.cumsum(np.random.randn(100)))
        er = efficiency_ratio(s, 20)
        valid = er.dropna()
        assert (valid >= 0).all()
        assert (valid <= 1.5).all()


class TestStrategyEngine:
    def test_no_trade_insufficient_data(self):
        engine = StrategyEngine()
        df = _make_daily_df(n=50)
        signal = engine.analyze("BTC/USD", df, df, 50000, 1000, [], 0)
        assert signal.signal_type == "NO_TRADE"

    def test_no_buy_in_panic(self):
        engine = StrategyEngine()
        df = _make_daily_df(n=300, trend=-0.005)
        signal = engine.analyze("BTC/USD", df, df, 30000, 1000, [], 0)
        assert signal.signal_type != "BUY" or signal.signal_type == "NO_TRADE"

    def test_sell_on_stop_loss(self):
        engine = StrategyEngine()
        df = _make_daily_df(n=300)
        positions = [{
            "symbol": "BTC/USD",
            "stop_loss": 49000,
            "entry_price": 50000,
            "risk_per_unit": 1000,
            "status": "open",
        }]
        signal = engine.analyze("BTC/USD", df, df, 48500, 1000, positions, 3.0)
        assert signal.signal_type == "SELL"

    def test_no_buy_when_balance_1110(self):
        engine = StrategyEngine()
        df = _make_daily_df(n=300, trend=0.002)
        signal = engine.analyze("BTC/USD", df, df, 50000, 1110, [], 0)
        assert signal.signal_type != "BUY"

    def test_no_buy_when_balance_below_975(self):
        engine = StrategyEngine()
        df = _make_daily_df(n=300, trend=0.002)
        signal = engine.analyze("BTC/USD", df, df, 50000, 970, [], 0)
        assert signal.signal_type != "BUY"

    def test_stale_data_no_signal(self):
        engine = StrategyEngine()
        signal = engine.analyze("BTC/USD", pd.DataFrame(), pd.DataFrame(), 50000, 1000, [], 0)
        assert signal.signal_type == "NO_TRADE"
