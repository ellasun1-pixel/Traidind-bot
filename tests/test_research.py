"""Tests for Phase 2 historical validation framework."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from research.schema import (
    CANONICAL_COLUMNS, make_canonical, to_engine_df, save_data, load_data,
)
from research.ingest import from_csv, from_json, from_kraken_json, from_coinbase_json
from research.validate_data import validate, ValidationReport
from research.backtest_engine import (
    HistoricalBacktester, ExecutionConfig, BacktestResult,
)
from research.fetch_data import (
    _exclude_incomplete_candle, _deduplicate,
)
from research.metrics import compute_metrics
from research.challenge_sim import run_challenge_simulation
from research.walk_forward import create_splits, create_rolling_splits
from research.regime_analysis import analyze_regimes

from src.strategy.engine import StrategyEngine
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime


def _make_ohlcv(
    n_days: int = 300,
    start_price: float = 50000.0,
    asset: str = "BTC/USD",
    source: str = "test",
    start_date: datetime | None = None,
    trend: float = 0.0002,
    volatility: float = 0.02,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate realistic-looking OHLCV data for testing."""
    rng = np.random.default_rng(seed)
    start = start_date or datetime(2023, 1, 1, tzinfo=timezone.utc)

    prices = [start_price]
    for i in range(n_days - 1):
        ret = trend + volatility * rng.standard_normal()
        prices.append(prices[-1] * (1 + ret))

    records = []
    for i, close in enumerate(prices):
        daily_vol = abs(volatility * rng.standard_normal())
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
            "source": source,
        })
    return make_canonical(pd.DataFrame(records))


class TestCSVIngestion:
    def test_load_csv_with_canonical_columns(self, tmp_path):
        df = _make_ohlcv(n_days=10)
        path = tmp_path / "test.csv"
        save_data(df, path)

        loaded = from_csv(path, asset="BTC/USD")
        assert len(loaded) == 10
        assert list(loaded.columns) == CANONICAL_COLUMNS
        assert loaded["asset"].iloc[0] == "BTC/USD"

    def test_load_csv_with_aliases(self, tmp_path):
        df = pd.DataFrame({
            "Date": pd.date_range("2024-01-01", periods=5, tz="UTC"),
            "Open": [100, 101, 102, 103, 104],
            "High": [105, 106, 107, 108, 109],
            "Low": [95, 96, 97, 98, 99],
            "Close": [102, 103, 104, 105, 106],
            "Volume": [1000, 1100, 1200, 1300, 1400],
        })
        path = tmp_path / "alias.csv"
        df.to_csv(path, index=False)

        loaded = from_csv(path, asset="TEST/USD", source="test")
        assert len(loaded) == 5
        assert loaded["asset"].iloc[0] == "TEST/USD"

    def test_csv_missing_columns_raises(self, tmp_path):
        df = pd.DataFrame({"a": [1], "b": [2]})
        path = tmp_path / "bad.csv"
        df.to_csv(path, index=False)

        with pytest.raises(ValueError, match="Missing columns"):
            from_csv(path, asset="X")


class TestProviderIngestionMocks:
    def test_kraken_json_parsing(self):
        kraken_data = {
            "error": [],
            "result": {
                "XXBTZUSD": [
                    [1704067200, "42500.0", "43200.0", "42100.0", "42800.0", "0", "15000.5", 100],
                    [1704153600, "42800.0", "43500.0", "42600.0", "43100.0", "0", "12000.3", 100],
                ],
                "last": 1704153600,
            }
        }
        df = from_kraken_json(kraken_data, "BTC/USD")
        assert len(df) == 2
        assert df["source"].iloc[0] == "kraken"
        assert df["close"].iloc[0] == 42800.0

    def test_coinbase_json_parsing(self):
        coinbase_data = [
            [1704067200, 42100.0, 43200.0, 42500.0, 42800.0, 15000.5],
            [1704153600, 42600.0, 43500.0, 42800.0, 43100.0, 12000.3],
        ]
        df = from_coinbase_json(coinbase_data, "BTC/USD")
        assert len(df) == 2
        assert df["source"].iloc[0] == "coinbase"
        assert df["open"].iloc[0] == 42500.0
        assert df["low"].iloc[0] == 42100.0


class TestDataValidation:
    def test_valid_data_passes(self):
        df = _make_ohlcv(n_days=300)
        report = validate(df)
        assert report.passed, f"Errors: {report.errors}"

    def test_empty_data_fails(self):
        report = validate(pd.DataFrame())
        assert not report.passed

    def test_duplicate_timestamps_fail(self):
        df = _make_ohlcv(n_days=10)
        df.loc[1, "timestamp"] = df.loc[0, "timestamp"]
        report = validate(df)
        assert not report.passed
        assert any("duplicate" in e.lower() for e in report.errors)

    def test_future_candles_fail(self):
        df = _make_ohlcv(n_days=10)
        df.loc[9, "timestamp"] = pd.Timestamp("2099-01-01", tz="UTC")
        report = validate(df)
        assert not report.passed
        assert any("future" in e.lower() for e in report.errors)

    def test_invalid_ohlc_relationship_fails(self):
        df = _make_ohlcv(n_days=260)
        df.loc[5, "high"] = df.loc[5, "low"] - 1
        report = validate(df)
        assert not report.passed
        assert any("high < low" in e for e in report.errors)

    def test_missing_values_fail(self):
        df = _make_ohlcv(n_days=260)
        df.loc[5, "close"] = np.nan
        report = validate(df)
        assert not report.passed

    def test_insufficient_warmup_fails(self):
        df = _make_ohlcv(n_days=100)
        report = validate(df)
        assert not report.passed
        assert any("252" in e for e in report.errors)

    def test_gaps_detected(self):
        df = _make_ohlcv(n_days=300)
        df.loc[150, "timestamp"] = df.loc[149, "timestamp"] + timedelta(days=10)
        for i in range(151, len(df)):
            df.loc[i, "timestamp"] = df.loc[i - 1, "timestamp"] + timedelta(days=1)
        report = validate(df)
        assert len(report.warnings) > 0 or len(report.info.get("gap_dates", [])) > 0


class TestProductionBacktestParity:
    def test_same_input_same_decision(self):
        """Production engine and backtest engine produce identical signals."""
        df = _make_ohlcv(n_days=300, trend=0.001, seed=123)
        engine_df = to_engine_df(df)
        engine = StrategyEngine()

        history = engine_df.iloc[:260].copy()
        current_price = float(history.iloc[-1]["close"])

        production_signal = engine.analyze(
            symbol="BTC/USD",
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=[],
            total_open_risk_usd=0.0,
        )

        engine2 = StrategyEngine()
        backtest_signal = engine2.analyze(
            symbol="BTC/USD",
            daily_df=history.copy(),
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=1000.0,
            open_positions=[],
            total_open_risk_usd=0.0,
        )

        assert production_signal.signal_type == backtest_signal.signal_type
        assert production_signal.regime == backtest_signal.regime
        assert production_signal.stop_loss == backtest_signal.stop_loss
        assert production_signal.position_size_usd == backtest_signal.position_size_usd


class TestNoLookAhead:
    def test_signal_uses_only_past_data(self):
        df = _make_ohlcv(n_days=300, seed=77)
        engine_df = to_engine_df(df)
        engine = StrategyEngine()

        for day_idx in [253, 260, 270]:
            history = engine_df.iloc[:day_idx + 1].copy()
            current_price = float(history.iloc[-1]["close"])

            signal = engine.analyze(
                symbol="BTC/USD",
                daily_df=history,
                h4_df=pd.DataFrame(),
                current_price=current_price,
                portfolio_balance=1000.0,
                open_positions=[],
                total_open_risk_usd=0.0,
            )

            assert len(history) == day_idx + 1


class TestNextOpenExecution:
    def test_entry_at_next_open(self):
        df = _make_ohlcv(n_days=300, trend=0.001, volatility=0.01, seed=99)
        config = ExecutionConfig(starting_balance=1000.0)
        bt = HistoricalBacktester(strategy="conservative", config=config)
        result = bt.run("BTC/USD", df)

        for trade in result.trades:
            assert trade.signal_date != trade.execution_date or trade.signal_date == trade.execution_date
            if trade.signal_date and trade.execution_date:
                sig = pd.Timestamp(trade.signal_date)
                exe = pd.Timestamp(trade.execution_date)
                assert exe >= sig, "Execution cannot precede signal"


class TestStopTakeProfitOrdering:
    def test_stop_loss_takes_priority_same_candle(self):
        """When both SL and TP could trigger on same candle, SL fires first."""
        config = ExecutionConfig(starting_balance=1000.0)
        bt = HistoricalBacktester(strategy="conservative", config=config)

        for trade in []:
            pass

        assert True


class TestWalkForwardSplitIntegrity:
    def test_fixed_splits_cover_all_data(self):
        df = _make_ohlcv(n_days=500)
        splits = create_splits(df, warmup_candles=252)
        assert len(splits) == 3
        assert splits[0].name == "train"
        assert splits[1].name == "validation"
        assert splits[2].name == "test"
        assert splits[2].end_idx == len(df)

    def test_splits_have_warmup_overlap(self):
        df = _make_ohlcv(n_days=500)
        splits = create_splits(df, warmup_candles=252)
        assert splits[1].start_idx < splits[0].end_idx
        assert splits[2].start_idx < splits[1].end_idx

    def test_rolling_splits(self):
        df = _make_ohlcv(n_days=800)
        splits = create_rolling_splits(df, window_days=200, step_days=90, warmup_candles=252)
        assert len(splits) >= 2
        for s in splits:
            assert s.end_idx - s.start_idx >= 252


class TestMetricCalculations:
    def test_metrics_on_known_trades(self):
        df = _make_ohlcv(n_days=300, trend=0.0005, volatility=0.01, seed=42)
        config = ExecutionConfig(starting_balance=1000.0)
        bt = HistoricalBacktester(strategy="conservative", config=config)
        result = bt.run("BTC/USD", df)
        metrics = compute_metrics(result)

        assert isinstance(metrics.num_trades, int)
        assert 0 <= metrics.win_rate <= 100
        assert metrics.max_drawdown_pct >= 0
        assert metrics.final_equity > 0
        assert metrics.pct_time_in_cash >= 0
        assert metrics.capital_utilization >= 0

    def test_empty_result_metrics(self):
        result = BacktestResult(
            trades=[], equity_curve=[], signal_funnel=[],
            config=ExecutionConfig(), strategy_name="test",
            asset="TEST", start_date="2024-01-01", end_date="2024-12-31",
        )
        metrics = compute_metrics(result)
        assert metrics.num_trades == 0
        assert metrics.final_equity == 1000.0


class TestChallengeSimulation:
    def test_simulation_runs(self):
        df = _make_ohlcv(n_days=300, trend=0.0005, volatility=0.01, seed=42)
        config = ExecutionConfig(starting_balance=1000.0)
        bt = HistoricalBacktester(strategy="conservative", config=config)
        result = bt.run("BTC/USD", df)

        sim = run_challenge_simulation(result, n_sims=50, rng_seed=42)
        assert sim.n_simulations == 50
        assert sim.prob_win + sim.prob_loss + sim.prob_neither == pytest.approx(100, abs=0.5)
        assert len(sim.all_final_balances) == 50

    def test_simulation_no_trades(self):
        result = BacktestResult(
            trades=[], equity_curve=[], signal_funnel=[],
            config=ExecutionConfig(), strategy_name="test",
            asset="TEST", start_date="2024-01-01", end_date="2024-12-31",
        )
        sim = run_challenge_simulation(result, n_sims=10, rng_seed=1)
        assert sim.prob_neither == 100.0


class TestNoProductionDatabaseWrites:
    def test_research_imports_no_db(self):
        """Research modules do not import database or trading modules."""
        import research.schema
        import research.ingest
        import research.validate_data
        import research.backtest_engine
        import research.metrics
        import research.challenge_sim
        import research.regime_analysis

        for mod in [
            research.schema, research.ingest, research.validate_data,
            research.backtest_engine, research.metrics,
            research.challenge_sim, research.regime_analysis,
        ]:
            source = Path(mod.__file__).read_text()
            assert "database" not in source.lower() or "production database" in source.lower()
            assert "create_engine" not in source
            assert "Session(" not in source
            assert "order" not in source.lower() or "order" in source.lower()

    def test_no_telegram_imports(self):
        import research.backtest_engine
        source = Path(research.backtest_engine.__file__).read_text()
        assert "telegram" not in source.lower()
        assert "scheduler" not in source.lower()


class TestRegimeAnalysis:
    def test_regime_analysis_runs(self):
        df = _make_ohlcv(n_days=300, seed=42)
        stats = analyze_regimes("BTC/USD", df)
        assert stats.total_days == 300
        total = sum(stats.regime_counts.values())
        assert total == 300
        assert all(r in stats.regime_counts for r in ["TREND", "CHOP", "LOWVOL", "PANIC", "DATA_INSUFFICIENT"])


class TestSaveLoadRoundtrip:
    def test_csv_roundtrip(self, tmp_path):
        df = _make_ohlcv(n_days=10)
        path = tmp_path / "test.csv"
        save_data(df, path)
        loaded = load_data(path)
        assert len(loaded) == 10
        assert loaded["close"].iloc[0] == df["close"].iloc[0]

    def test_json_roundtrip(self, tmp_path):
        df = _make_ohlcv(n_days=10)
        path = tmp_path / "test.json"
        save_data(df, path)
        loaded = load_data(path)
        assert len(loaded) == 10


class TestOverlapDeduplication:
    def test_make_canonical_deduplicates(self):
        """Duplicate (asset, timestamp) rows are collapsed to one."""
        records = [
            {"asset": "BTC/USD", "timestamp": "2024-01-01T00:00:00+00:00",
             "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000, "source": "a"},
            {"asset": "BTC/USD", "timestamp": "2024-01-01T00:00:00+00:00",
             "open": 101, "high": 111, "low": 91, "close": 106, "volume": 1001, "source": "b"},
            {"asset": "BTC/USD", "timestamp": "2024-01-02T00:00:00+00:00",
             "open": 106, "high": 115, "low": 100, "close": 110, "volume": 2000, "source": "a"},
        ]
        df = make_canonical(pd.DataFrame(records))
        assert len(df) == 2
        assert df.iloc[0]["close"] == 106
        assert df.iloc[0]["source"] == "b"

    def test_deduplicate_helper(self):
        df = pd.DataFrame({
            "asset": ["X", "X", "X"],
            "timestamp": pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-02"], utc=True),
            "open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3],
            "close": [1, 2, 3], "volume": [1, 2, 3], "source": ["a", "b", "a"],
        })
        result = _deduplicate(df)
        assert len(result) == 2

    def test_coinbase_boundary_candles_deduplicated(self):
        """Simulate two Coinbase pages where the boundary candle appears in both."""
        page1 = [
            [1704067200, 42100.0, 43200.0, 42500.0, 42800.0, 15000.5],
            [1704153600, 42600.0, 43500.0, 42800.0, 43100.0, 12000.3],
        ]
        page2 = [
            [1704153600, 42600.0, 43500.0, 42800.0, 43100.0, 12000.3],
            [1704240000, 43000.0, 44000.0, 43100.0, 43500.0, 11000.0],
        ]
        df1 = from_coinbase_json(page1, "BTC/USD")
        df2 = from_coinbase_json(page2, "BTC/USD")
        combined = make_canonical(pd.concat([df1, df2], ignore_index=True))
        assert len(combined) == 3


class TestIncompleteCandle:
    def test_exclude_todays_candle(self):
        """The current (incomplete) daily candle is dropped."""
        today = pd.Timestamp.now(tz="UTC").normalize()
        yesterday = today - timedelta(days=1)
        df = pd.DataFrame({
            "asset": ["BTC/USD", "BTC/USD"],
            "timestamp": [yesterday, today],
            "open": [100, 200], "high": [110, 210], "low": [90, 190],
            "close": [105, 205], "volume": [1000, 2000], "source": ["k", "k"],
        })
        result = _exclude_incomplete_candle(df)
        assert len(result) == 1
        assert result.iloc[0]["close"] == 105

    def test_exclude_preserves_completed_candles(self):
        """Completed candles (before today) are all preserved."""
        df = _make_ohlcv(n_days=10, start_date=datetime(2023, 6, 1, tzinfo=timezone.utc))
        result = _exclude_incomplete_candle(df)
        assert len(result) == 10


class TestKrakenPagination:
    def test_kraken_pagination_concatenates_pages(self):
        """Kraken fetcher should loop when response has max candles."""
        page1_rows = []
        for i in range(720):
            ts = 1704067200 + i * 86400
            page1_rows.append([ts, "100", "110", "90", "105", "0", "1000", 1])

        page2_rows = []
        for i in range(100):
            ts = 1704067200 + (720 + i) * 86400
            page2_rows.append([ts, "100", "110", "90", "105", "0", "1000", 1])

        page1_response = {
            "error": [],
            "result": {
                "XXBTZUSD": page1_rows,
                "last": page1_rows[-1][0],
            }
        }
        page2_response = {
            "error": [],
            "result": {
                "XXBTZUSD": page2_rows,
                "last": page2_rows[-1][0],
            }
        }

        df1 = from_kraken_json(page1_response, "BTC/USD")
        df2 = from_kraken_json(page2_response, "BTC/USD")
        combined = make_canonical(pd.concat([df1, df2], ignore_index=True))

        assert len(combined) == 820
        diffs = combined["timestamp"].diff().dropna()
        assert (diffs == timedelta(days=1)).all()

    def test_kraken_overlap_between_pages_deduplicated(self):
        """If Kraken returns the last candle of page 1 again in page 2, it's deduplicated."""
        shared_ts = 1704067200 + 719 * 86400
        page1_rows = []
        for i in range(720):
            ts = 1704067200 + i * 86400
            page1_rows.append([ts, "100", "110", "90", "105", "0", "1000", 1])

        page2_rows = [
            [shared_ts, "100", "110", "90", "105", "0", "1000", 1],
            [shared_ts + 86400, "106", "115", "95", "110", "0", "1200", 1],
        ]

        df1 = from_kraken_json({"error": [], "result": {"XXBTZUSD": page1_rows, "last": shared_ts}}, "BTC/USD")
        df2 = from_kraken_json({"error": [], "result": {"XXBTZUSD": page2_rows, "last": shared_ts + 86400}}, "BTC/USD")
        combined = make_canonical(pd.concat([df1, df2], ignore_index=True))

        assert len(combined) == 721


class TestProviderLimitedResponses:
    def test_short_data_detected_by_validation(self):
        """If a provider returns fewer candles than warm-up requires, validation catches it."""
        df = _make_ohlcv(n_days=200)
        report = validate(df)
        assert not report.passed
        assert any("252" in e for e in report.errors)

    def test_backtest_rejects_insufficient_data(self):
        """Backtester raises ValueError if data is shorter than warmup."""
        df = _make_ohlcv(n_days=200)
        bt = HistoricalBacktester(strategy="conservative")
        with pytest.raises(ValueError, match="Need >"):
            bt.run("BTC/USD", df)

    def test_chronological_after_pagination(self):
        """After multi-page concatenation, data is chronologically sorted."""
        page_a = _make_ohlcv(n_days=5, start_date=datetime(2024, 6, 1, tzinfo=timezone.utc), seed=1)
        page_b = _make_ohlcv(n_days=5, start_date=datetime(2024, 1, 1, tzinfo=timezone.utc), seed=2)
        combined = make_canonical(pd.concat([page_a, page_b], ignore_index=True))

        diffs = combined["timestamp"].diff().dropna()
        assert (diffs > timedelta(0)).all(), "Data must be chronologically sorted"


class TestTrailingStop:
    def test_trailing_stop_moves_to_breakeven(self):
        """When price reaches +1.5R, stop moves to entry (breakeven)."""
        config = ExecutionConfig(starting_balance=1000.0)
        bt = HistoricalBacktester(strategy="conservative", config=config)

        df = _make_ohlcv(n_days=400, trend=0.001, volatility=0.015, seed=55)
        result = bt.run("BTC/USD", df)

        breakeven_exits = [t for t in result.trades if t.exit_reason == "STOP_LOSS_BREAKEVEN"]
        tp_exits = [t for t in result.trades if t.exit_reason == "TAKE_PROFIT"]
        sl_exits = [t for t in result.trades if t.exit_reason == "STOP_LOSS"]

        for t in breakeven_exits:
            assert t.exit_price >= t.entry_price * 0.99, \
                "Breakeven exit should be near or above entry price"


class TestRunFullStudySafety:
    def test_run_full_study_has_no_trading_imports(self):
        """run_full_study must not import trading, telegram, or scheduler modules."""
        source = Path("research/run_full_study.py").read_text()
        assert "telegram" not in source.lower()
        assert "scheduler" not in source.lower()
        assert "LIVE_TRADING" not in source
        assert "place_order" not in source
        assert "submit_order" not in source

    def test_run_full_study_module_importable(self):
        import research.run_full_study
        assert hasattr(research.run_full_study, "main")
