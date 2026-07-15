"""Tests for the research study code — validates that research components work
correctly without modifying production code."""
from __future__ import annotations

import numpy as np
import pytest

from research.data_generator import generate_asset_data, generate_all_assets, ASSET_PROFILES
from research.enhanced_backtester import run_enhanced_backtest, DetailedTrade
from research.experimental_engines import (
    ExperimentB_NoCHOP,
    ExperimentC_TrendOnly,
    ExperimentD_TP2R,
    ExperimentE_TP1_5R,
    ExperimentF_GraduatedRisk,
    ExperimentG_EarlyTrend,
)
from src.strategy.engine import StrategyEngine


class TestDataGenerator:
    def test_generates_correct_length(self):
        df = generate_asset_data("BTC/USD", n_days=500)
        assert len(df) == 500

    def test_has_required_columns(self):
        df = generate_asset_data("BTC/USD", n_days=300)
        for col in ["open_time", "open", "high", "low", "close", "volume"]:
            assert col in df.columns

    def test_ohlc_consistency(self):
        df = generate_asset_data("BTC/USD", n_days=300)
        assert (df["high"] >= df["low"]).all()
        assert (df["high"] >= df["close"]).all()
        assert (df["high"] >= df["open"]).all()
        assert (df["low"] <= df["close"]).all()
        assert (df["low"] <= df["open"]).all()

    def test_all_assets_generated(self):
        data = generate_all_assets(n_days=250)
        assert len(data) == 5
        for symbol in ASSET_PROFILES:
            assert symbol in data

    def test_deterministic_with_seed(self):
        df1 = generate_asset_data("BTC/USD", n_days=100, seed=42)
        df2 = generate_asset_data("BTC/USD", n_days=100, seed=42)
        assert (df1["close"] == df2["close"]).all()


class TestEnhancedBacktester:
    def test_returns_detailed_trades(self):
        engine = StrategyEngine()
        df = generate_asset_data("BTC/USD", n_days=400, seed=99)
        result = run_enhanced_backtest(engine, "BTC/USD", df, "Test")
        for t in result.trades:
            assert isinstance(t, DetailedTrade)
            assert t.symbol == "BTC/USD"
            assert t.regime_at_entry in ("TREND", "CHOP", "LOWVOL", "PANIC")

    def test_tracks_excursions(self):
        engine = StrategyEngine()
        df = generate_asset_data("BTC/USD", n_days=400, seed=99)
        result = run_enhanced_backtest(engine, "BTC/USD", df, "Test")
        for t in result.trades:
            assert t.max_favorable_excursion >= 0
            assert t.max_adverse_excursion >= 0

    def test_too_few_candles(self):
        engine = StrategyEngine()
        df = generate_asset_data("BTC/USD", n_days=100)
        result = run_enhanced_backtest(engine, "BTC/USD", df, "Test")
        assert result.total_trades == 0


class TestExperimentalEngines:
    def test_no_chop_blocks_chop(self):
        engine = ExperimentB_NoCHOP()
        df = generate_asset_data("BTC/USD", n_days=400, seed=99)
        result = run_enhanced_backtest(engine, "BTC/USD", df, "NoCHOP")
        for t in result.trades:
            assert t.regime_at_entry != "CHOP"

    def test_trend_only_blocks_chop_and_lowvol(self):
        engine = ExperimentC_TrendOnly()
        df = generate_asset_data("BTC/USD", n_days=400, seed=99)
        result = run_enhanced_backtest(engine, "BTC/USD", df, "TrendOnly")
        for t in result.trades:
            assert t.regime_at_entry not in ("CHOP", "LOWVOL")

    def test_tp_2r_uses_lower_multiple(self):
        engine = ExperimentD_TP2R()
        assert engine.take_profit_multiple == 2.0

    def test_tp_1_5r_uses_lower_multiple(self):
        engine = ExperimentE_TP1_5R()
        assert engine.take_profit_multiple == 1.5

    def test_all_engines_produce_results(self):
        engines = [
            ExperimentB_NoCHOP(),
            ExperimentC_TrendOnly(),
            ExperimentD_TP2R(),
            ExperimentE_TP1_5R(),
            ExperimentF_GraduatedRisk(),
            ExperimentG_EarlyTrend(),
        ]
        df = generate_asset_data("BTC/USD", n_days=400, seed=55)
        for engine in engines:
            result = run_enhanced_backtest(engine, "BTC/USD", df, "Test")
            assert result.final_balance > 0
            assert result.days_simulated > 0
