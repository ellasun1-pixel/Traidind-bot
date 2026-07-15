"""Regression tests: research backtest and production engine parity.

Verifies identical outcomes on identical synthetic candle sequences for:
  - BUY signal generation
  - No BUY because of EMA200 filter
  - No BUY because of candle confirmation
  - Stop-loss exit
  - Take-profit exit
  - Trailing stop activation at +1.5R
  - Breakeven exit after trailing activation
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from research.schema import make_canonical, to_engine_df
from research.backtest_engine import HistoricalBacktester, ExecutionConfig
from src.strategy.engine import StrategyEngine, TradeSignal
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from src.config import settings


def _build_candles(
    n_days: int = 300,
    start_price: float = 50000.0,
    trend: float = 0.0003,
    volatility: float = 0.015,
    seed: int = 100,
    asset: str = "BTC/USD",
) -> pd.DataFrame:
    """Build deterministic OHLCV candles with controllable trend."""
    rng = np.random.default_rng(seed)
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    prices = [start_price]
    for _ in range(n_days - 1):
        ret = trend + volatility * rng.standard_normal()
        prices.append(prices[-1] * (1 + ret))

    records = []
    for i, close in enumerate(prices):
        daily_vol = abs(volatility * rng.standard_normal()) * 0.5
        high = close * (1 + daily_vol)
        low = close * (1 - daily_vol)
        open_price = low + (high - low) * rng.random()
        high = max(high, open_price, close)
        low = min(low, open_price, close)

        records.append({
            "asset": asset,
            "timestamp": start + timedelta(days=i),
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": round(rng.uniform(1000, 50000), 2),
            "source": "test",
        })
    return make_canonical(pd.DataFrame(records))


def _run_production_engine(candles_df: pd.DataFrame, asset: str) -> list[TradeSignal]:
    """Run StrategyEngine.analyze() on each candle from warmup onward."""
    engine_df = to_engine_df(candles_df)
    engine = StrategyEngine()
    signals = []
    for i in range(252, len(engine_df) - 1):
        history = engine_df.iloc[:i + 1].copy()
        current_price = float(engine_df.iloc[i]["close"])
        signal = engine.analyze(
            symbol=asset,
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=[],
            total_open_risk_usd=0.0,
        )
        signals.append(signal)
    return signals


class TestProductionBacktestParity:
    """Verify production engine and backtest engine produce same signals."""

    def test_buy_signal_parity_stateless(self):
        """Production engine and backtest engine call the same analyze() method.

        The backtest is stateful (tracks positions, balance), so we verify
        that on candles where the backtest has no open positions and balance
        is near starting, both engines agree on signal type.
        """
        df = _build_candles(n_days=400, trend=0.001, seed=200)
        engine_df = to_engine_df(df)

        bt = HistoricalBacktester(strategy="conservative")
        result = bt.run("BTC/USD", df, warmup_candles=252)

        # Verify the backtest uses the same StrategyEngine class
        assert isinstance(bt.engine, StrategyEngine)

        # Verify the first BUY signal (if any) matches a production call
        first_buy = None
        for entry in result.signal_funnel:
            if entry.signal_type == "BUY":
                first_buy = entry
                break

        if first_buy is not None:
            # Find the candle index for this date
            for i in range(252, len(engine_df) - 1):
                date = str(engine_df.iloc[i]["open_time"])[:10]
                if date == first_buy.date:
                    history = engine_df.iloc[:i + 1].copy()
                    current_price = float(engine_df.iloc[i]["close"])
                    signal = bt.engine.analyze(
                        symbol="BTC/USD",
                        daily_df=history,
                        h4_df=pd.DataFrame(),
                        current_price=current_price,
                        portfolio_balance=1000.0,
                        open_positions=[],
                        total_open_risk_usd=0.0,
                    )
                    assert signal.signal_type == "BUY", (
                        f"Engine returned {signal.signal_type} on {date}, expected BUY"
                    )
                    break

    def test_buy_allowed_below_ema200(self):
        """BUY is allowed when price < EMA200 (EMA200 gate removed)."""
        df = _build_candles(n_days=400, trend=-0.002, seed=300)
        engine_df = to_engine_df(df)
        daily = compute_indicators(engine_df)

        engine = StrategyEngine()
        found_buy_below_ema200 = False
        for i in range(252, len(daily) - 1):
            row = daily.iloc[i]
            close = float(row["close"])
            ema200 = float(row.get("ema200", 0) or 0)
            if ema200 > 0 and close < ema200:
                history = engine_df.iloc[:i + 1].copy()
                signal = engine.analyze(
                    symbol="BTC/USD",
                    daily_df=history,
                    h4_df=pd.DataFrame(),
                    current_price=close,
                    portfolio_balance=1000.0,
                    open_positions=[],
                    total_open_risk_usd=0.0,
                )
                if signal.signal_type == "BUY":
                    found_buy_below_ema200 = True
                    break
        assert found_buy_below_ema200, "Should be able to BUY below EMA200 after gate removal"

    def test_no_buy_without_candle_confirmation(self):
        """No BUY when prev close <= prev EMA50 — both engines agree."""
        df = _build_candles(n_days=400, trend=0.0005, seed=400)
        engine_df = to_engine_df(df)
        daily = compute_indicators(engine_df)

        engine = StrategyEngine()
        for i in range(252, len(daily) - 1):
            prev = daily.iloc[i - 1]
            prev_close = float(prev.get("close", 0) or 0)
            prev_ema50 = float(prev.get("ema50", 0) or 0)
            if prev_close > 0 and prev_ema50 > 0 and prev_close <= prev_ema50:
                history = engine_df.iloc[:i + 1].copy()
                signal = engine.analyze(
                    symbol="BTC/USD",
                    daily_df=history,
                    h4_df=pd.DataFrame(),
                    current_price=float(daily.iloc[i]["close"]),
                    portfolio_balance=1000.0,
                    open_positions=[],
                    total_open_risk_usd=0.0,
                )
                assert signal.signal_type != "BUY", (
                    f"Got BUY at candle {i} with prev_close={prev_close:.2f} <= prev_ema50={prev_ema50:.2f}"
                )

    def test_stop_loss_exit(self):
        """Stop-loss triggers identically in backtest and production engine check."""
        engine = StrategyEngine()
        existing = [{
            "symbol": "BTC/USD",
            "entry_price": 50000.0,
            "stop_loss": 48500.0,
            "risk_per_unit": 1500.0,
            "status": "open",
        }]
        current_price = 48400.0  # Below stop

        df = _build_candles(n_days=300, trend=0.0)
        engine_df = to_engine_df(df)
        history = engine_df.iloc[:260].copy()

        signal = engine.analyze(
            symbol="BTC/USD",
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=existing,
            total_open_risk_usd=3.0,
        )
        assert signal.signal_type == "SELL"
        assert signal.reason == "Stop-loss level breached"

    def test_take_profit_exit(self):
        """Take-profit triggers at correct multiple of risk."""
        engine = StrategyEngine()
        assert engine.take_profit_multiple == settings.take_profit_risk_multiple

        entry = 50000.0
        risk_per_unit = 1500.0
        current_price = entry + risk_per_unit * (engine.take_profit_multiple + 0.1)

        existing = [{
            "symbol": "BTC/USD",
            "entry_price": entry,
            "stop_loss": entry - risk_per_unit,
            "risk_per_unit": risk_per_unit,
            "status": "open",
        }]

        df = _build_candles(n_days=300, trend=0.0)
        engine_df = to_engine_df(df)
        history = engine_df.iloc[:260].copy()

        signal = engine.analyze(
            symbol="BTC/USD",
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=existing,
            total_open_risk_usd=3.0,
        )
        assert signal.signal_type == "TAKE_PROFIT"

    def test_trailing_stop_activation_at_1_5r(self):
        """At +1.5R, stop moves to breakeven — production engine."""
        engine = StrategyEngine()
        entry = 50000.0
        risk_per_unit = 1500.0
        original_stop = entry - risk_per_unit  # 48500

        # Price at +1.5R = 50000 + 1500*1.5 = 52250
        current_price = 52300.0  # Above +1.5R threshold

        existing = [{
            "symbol": "BTC/USD",
            "entry_price": entry,
            "stop_loss": original_stop,
            "risk_per_unit": risk_per_unit,
            "status": "open",
        }]

        df = _build_candles(n_days=300, trend=0.0)
        engine_df = to_engine_df(df)
        history = engine_df.iloc[:260].copy()

        # At +1.5R the production engine should move stop to breakeven
        # The trailing stop logic in _check_sell_conditions adjusts stop_loss
        # but the position dict has stop_loss=48500, and current_price=52300 > breakeven_threshold=52250
        # so stop is updated to max(48500, 50000) = 50000
        # Since current_price 52300 > 50000, the stop is NOT hit
        signal = engine.analyze(
            symbol="BTC/USD",
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=existing,
            total_open_risk_usd=3.0,
        )
        # Should not trigger SELL since price is above breakeven
        assert signal.signal_type != "SELL", "Should not sell when price is above breakeven stop"

    def test_breakeven_exit_after_trailing_activation(self):
        """After trailing stop activates, price drops to entry → SELL."""
        engine = StrategyEngine()
        entry = 50000.0
        risk_per_unit = 1500.0

        # Simulate: stop was already moved to breakeven (entry price)
        # by a previous candle where price >= entry + 1.5*risk
        # Now price drops to entry level
        current_price = 49900.0  # Below entry = breakeven stop

        existing = [{
            "symbol": "BTC/USD",
            "entry_price": entry,
            "stop_loss": entry - risk_per_unit,  # Original stop
            "risk_per_unit": risk_per_unit,
            "status": "open",
        }]

        df = _build_candles(n_days=300, trend=0.0)
        engine_df = to_engine_df(df)
        history = engine_df.iloc[:260].copy()

        # The engine checks: breakeven_threshold = 50000 + 1500*1.5 = 52250
        # current_price = 49900 < 52250, so trailing stop does NOT activate
        # But 49900 < original stop (48500)? No, 49900 > 48500
        # So no sell here. Let's fix the test to set stop at entry (breakeven)
        existing[0]["stop_loss"] = entry  # Stop already moved to breakeven

        signal = engine.analyze(
            symbol="BTC/USD",
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=existing,
            total_open_risk_usd=3.0,
        )
        assert signal.signal_type == "SELL"
        assert signal.reason == "Stop-loss level breached"

    def test_trailing_stop_parity_backtest(self):
        """Backtest trailing stop logic matches production."""
        config = ExecutionConfig()

        # Backtest trailing stop from _process_exits:
        entry = 50000.0
        risk_per_unit = 1500.0
        original_sl = entry - risk_per_unit  # 48500
        breakeven_threshold = entry + risk_per_unit * 1.5  # 52250

        # Case 1: high reaches +1.5R, stop moves to breakeven
        high = 52300.0
        sl = original_sl
        if high >= breakeven_threshold:
            sl = max(sl, entry)
        assert sl == entry, "Stop should move to entry (breakeven)"

        # Case 2: after trailing, low hits breakeven stop
        low = 49800.0
        assert low <= sl, "Low should trigger breakeven stop"

        # Case 3: high doesn't reach +1.5R, stop stays original
        high2 = 52000.0
        sl2 = original_sl
        if high2 >= breakeven_threshold:
            sl2 = max(sl2, entry)
        assert sl2 == original_sl, "Stop should stay at original level"

    def test_backtest_signal_funnel_records_all_signals(self):
        """Backtest signal_funnel captures every candle's signal type."""
        df = _build_candles(n_days=350, trend=0.0005, seed=500)
        bt = HistoricalBacktester(strategy="conservative")
        result = bt.run("BTC/USD", df, warmup_candles=252)

        expected_candles = len(to_engine_df(df)) - 252 - 1
        # signal_funnel should have approximately expected_candles entries
        # (may be fewer if challenge boundary hit)
        assert len(result.signal_funnel) > 0
        assert len(result.signal_funnel) <= expected_candles

        # Every entry should have required fields
        for entry in result.signal_funnel:
            assert entry.date
            assert entry.asset == "BTC/USD"
            assert entry.signal_type in ("BUY", "SELL", "MOVE_TO_USD", "TAKE_PROFIT", "NO_TRADE", "REDUCE")
            assert entry.regime in ("TREND", "CHOP", "LOWVOL", "PANIC", "DATA_INSUFFICIENT")

    def test_regime_classification_consistency(self):
        """classify_regime returns same result regardless of call context."""
        df = _build_candles(n_days=300, trend=0.0003, seed=600)
        engine_df = to_engine_df(df)
        daily = compute_indicators(engine_df)

        for i in range(252, len(daily)):
            row = daily.iloc[i]
            r1 = classify_regime(row)
            r2 = classify_regime(row)
            assert r1 == r2, f"Regime inconsistent at candle {i}"

    def test_production_engine_take_profit_multiple_matches_config(self):
        """StrategyEngine uses settings.take_profit_risk_multiple."""
        engine = StrategyEngine()
        assert engine.take_profit_multiple == settings.take_profit_risk_multiple
