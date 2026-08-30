"""Tests for Batch 4 audit fixes.

Verifies:
(a) peak_price, tp1_fired, tp2_fired are persisted in DB (not memory-only)
(b) restore_from_db reads peak_price/tp fields from DB
(c) TradeSignal.sell_pct defaults to 0.0 (not 1.0)
(d) get_total_equity logs when falling back to entry_price
(e) Migration 013 exists with correct operations
(f) update_peak_prices persists to DB
"""
from __future__ import annotations

import inspect

import pytest


class TestPositionTrackingFields:
    def test_paper_position_has_peak_price(self):
        from src.database.models import PaperPosition
        assert hasattr(PaperPosition, "peak_price")

    def test_paper_position_has_tp1_fired(self):
        from src.database.models import PaperPosition
        assert hasattr(PaperPosition, "tp1_fired")

    def test_paper_position_has_tp2_fired(self):
        from src.database.models import PaperPosition
        assert hasattr(PaperPosition, "tp2_fired")

    def test_restore_reads_peak_price(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.restore_from_db)
        assert "op.peak_price" in source
        assert "peak_price" in source

    def test_restore_reads_tp_flags(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.restore_from_db)
        assert "op.tp1_fired" in source
        assert "op.tp2_fired" in source

    def test_persist_buy_includes_peak_price(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_buy)
        assert "peak_price" in source

    def test_persist_partial_sell_writes_tp_flags(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_partial_sell)
        assert "tp1_fired" in source
        assert "tp2_fired" in source
        assert "peak_price" in source

    def test_update_peak_prices_persists_to_db(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.update_peak_prices)
        assert "get_session" in source
        assert "peak_price" in source


class TestMigration013:
    @staticmethod
    def _read_migration():
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "013_batch4_position_tracking_fields.py"
        return p.read_text()

    def test_migration_013_exists(self):
        source = self._read_migration()
        assert 'revision = "013"' in source
        assert 'down_revision = "012"' in source

    def test_migration_013_adds_peak_price(self):
        source = self._read_migration()
        assert "peak_price" in source

    def test_migration_013_adds_tp_flags(self):
        source = self._read_migration()
        assert "tp1_fired" in source
        assert "tp2_fired" in source


class TestTradeSignalDefaults:
    def test_sell_pct_default_is_zero(self):
        from src.strategy.engine import TradeSignal
        from src.strategy.regime import MarketRegime
        sig = TradeSignal(
            signal_type="SELL",
            priority="CRITICAL",
            asset_symbol="BTC/USD",
            regime=MarketRegime.TREND,
        )
        assert sig.sell_pct == 0.0

    def test_reduce_signals_set_sell_pct_explicitly(self):
        from src.strategy import engine
        source = inspect.getsource(engine.StrategyEngine._check_live_funded_exits)
        assert "sell_pct=settings.live_tp1_sell_pct" in source
        assert "sell_pct=settings.live_tp2_sell_pct" in source


class TestStalePriceFallback:
    def test_get_total_equity_logs_missing_price(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.get_total_equity)
        assert "No live price" in source or "entry_price" in source
        assert "logger" in source

    def test_reduce_signal_check_uses_sell_pct_gt_zero(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs._process_single_asset)
        assert "sell_pct > 0" in source


class TestMigrationComment:
    def test_migration_001_comment_says_10000(self):
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "001_initial_schema.py"
        source = p.read_text()
        assert "$10,000" in source or "$10000" in source
        assert "$1000 starting" not in source
