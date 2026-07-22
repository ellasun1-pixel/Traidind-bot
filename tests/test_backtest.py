"""
Backtest comparison tests — validates the backtesting framework and runs
Conservative vs Challenge strategy comparison on synthetic data.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.engine import StrategyEngine
from src.strategy.challenge_engine import ChallengeStrategyEngine
from src.strategy.backtester import (
    run_backtest, run_monte_carlo, compare_strategies, BacktestResult,
)


def _make_market_df(
    n: int = 500,
    base_price: float = 50000.0,
    trend: float = 0.001,
    volatility: float = 0.02,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    returns = rng.normal(trend, volatility, n)
    closes = base_price * np.cumprod(1 + returns)
    highs = closes * (1 + rng.uniform(0.001, 0.015, n))
    lows = closes * (1 - rng.uniform(0.001, 0.015, n))
    opens = np.roll(closes, 1)
    opens[0] = base_price
    dates = pd.date_range("2025-01-01", periods=n, freq="1D")
    return pd.DataFrame({
        "open_time": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": rng.uniform(500, 2000, n),
    })


class TestBacktestFramework:
    def test_backtest_returns_result(self):
        engine = StrategyEngine()
        df = _make_market_df(300)
        result = run_backtest(engine, "BTC/USD", df, "Conservative")
        assert isinstance(result, BacktestResult)
        assert result.days_simulated > 0
        assert result.final_balance > 0

    def test_backtest_too_few_candles(self):
        engine = StrategyEngine()
        df = _make_market_df(100)
        result = run_backtest(engine, "BTC/USD", df, "Conservative")
        assert result.total_trades == 0
        assert result.days_simulated == 0

    def test_equity_curve_length(self):
        engine = StrategyEngine()
        df = _make_market_df(300)
        result = run_backtest(engine, "BTC/USD", df, "Conservative")
        assert len(result.equity_curve) > 0

    def test_challenge_engine_backtest(self):
        engine = ChallengeStrategyEngine()
        df = _make_market_df(300)
        result = run_backtest(engine, "BTC/USD", df, "Challenge")
        assert isinstance(result, BacktestResult)
        assert result.strategy_name == "Challenge"

    def test_win_loss_add_up(self):
        engine = StrategyEngine()
        df = _make_market_df(500, trend=0.002, seed=99)
        result = run_backtest(engine, "BTC/USD", df, "Conservative")
        assert result.wins + result.losses == result.total_trades

    def test_max_drawdown_nonnegative(self):
        engine = StrategyEngine()
        df = _make_market_df(300)
        result = run_backtest(engine, "BTC/USD", df, "Conservative")
        assert result.max_drawdown_pct >= 0


class TestMonteCarloAndComparison:
    def test_monte_carlo_runs(self):
        engine = StrategyEngine()
        df = _make_market_df(500, seed=7)
        mc = run_monte_carlo(engine, "BTC/USD", df, "Conservative", n_simulations=10, window_days=60)
        assert mc["n_simulations"] == 10
        assert 0.0 <= mc["pass_rate"] <= 1.0
        assert 0.0 <= mc["fail_rate"] <= 1.0

    def test_compare_strategies_returns_both(self):
        con = StrategyEngine()
        cha = ChallengeStrategyEngine()
        df = _make_market_df(500, seed=7)
        result = compare_strategies(con, cha, "BTC/USD", df, n_simulations=5, window_days=60)
        assert "conservative" in result
        assert "challenge" in result
        assert result["conservative"]["strategy_name"] == "Conservative"
        assert result["challenge"]["strategy_name"] == "Challenge"


class TestChallengeEngineConfig:
    def test_max_open_positions_is_2(self):
        """Fix #6: challenge_engine must use max_open_positions=2, matching the spec."""
        from src.strategy.challenge_engine import ChallengeConfig
        cfg = ChallengeConfig()
        assert cfg.max_open_positions == 2, (
            f"ChallengeConfig.max_open_positions={cfg.max_open_positions}, spec requires 2"
        )
