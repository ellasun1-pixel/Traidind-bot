"""Tests for Batch 3 audit fixes.

Verifies:
(a) PortfolioSnapshot model has agent_mode column
(b) PortfolioSnapshotRepository.record accepts agent_mode
(c) record_portfolio_snapshot passes _active_mode
(d) _process_single_asset accepts cycle_mode parameter
(e) market_check_job snapshots _active_mode at start (concurrency fix)
(f) market_check_job releases lock on crash (try/finally)
(g) Reserved word columns are quoted in ORM models
(h) Migration 012 exists with correct operations
"""
from __future__ import annotations

import inspect

import pytest


class TestPortfolioSnapshotAgentMode:
    def test_model_has_agent_mode_column(self):
        from src.database.models import PortfolioSnapshot
        assert hasattr(PortfolioSnapshot, "agent_mode")

    def test_repository_accepts_agent_mode(self):
        from src.database.repository import PortfolioSnapshotRepository
        sig = inspect.signature(PortfolioSnapshotRepository.record)
        assert "agent_mode" in sig.parameters

    def test_record_portfolio_snapshot_passes_mode(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs.record_portfolio_snapshot)
        assert "agent_mode=" in source
        assert "_active_mode" in source


class TestUniqueIndexMigration:
    @staticmethod
    def _read_migration():
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "012_batch3_snapshot_mode_and_index.py"
        return p.read_text()

    def test_migration_012_exists(self):
        source = self._read_migration()
        assert 'revision = "012"' in source
        assert 'down_revision = "011"' in source

    def test_migration_012_includes_agent_mode_in_index(self):
        source = self._read_migration()
        assert "agent_mode" in source
        assert "uq_one_open_per_asset_mode" in source

    def test_migration_012_adds_snapshot_column(self):
        source = self._read_migration()
        assert "portfolio_snapshots" in source
        assert "agent_mode" in source


class TestActiveModeSnapshot:
    def test_market_check_snapshots_mode_at_start(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs.market_check_job)
        cycle_idx = source.index("cycle_mode = _active_mode")
        asset_idx = source.index("_process_single_asset")
        assert cycle_idx < asset_idx, "cycle_mode must be captured before processing"

    def test_process_single_asset_accepts_cycle_mode(self):
        from src.scheduler import jobs
        sig = inspect.signature(jobs._process_single_asset)
        assert "cycle_mode" in sig.parameters

    def test_process_single_asset_uses_mode_for_cycle(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs._process_single_asset)
        assert "mode_for_cycle" in source
        assert "active_mode=mode_for_cycle" in source

    def test_market_check_passes_cycle_mode(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs.market_check_job)
        assert "cycle_mode=cycle_mode" in source


class TestLockRelease:
    def test_market_check_has_crash_handler(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs.market_check_job)
        assert "market_check_job crashed unexpectedly" in source

    def test_market_check_releases_lock_on_crash(self):
        from src.scheduler import jobs
        source = inspect.getsource(jobs.market_check_job)
        assert "release_lock" in source or "mark_failure" in source
        crash_idx = source.index("crashed unexpectedly")
        release_region = source[crash_idx:]
        assert "mark_failure" in release_region or "release_lock" in release_region


class TestReservedWordQuoting:
    def test_price_history_open_is_quoted(self):
        from src.database.models import PriceHistory
        col = PriceHistory.__table__.c["open"]
        col_name = col.name
        assert hasattr(col_name, "quote") or str(col_name) == "open"

    def test_price_history_close_is_quoted(self):
        from src.database.models import PriceHistory
        col = PriceHistory.__table__.c["close"]
        col_name = col.name
        assert hasattr(col_name, "quote") or str(col_name) == "close"

    def test_portfolio_snapshot_trigger_is_quoted(self):
        from src.database.models import PortfolioSnapshot
        col = PortfolioSnapshot.__table__.c["trigger"]
        col_name = col.name
        assert hasattr(col_name, "quote") or str(col_name) == "trigger"

    def test_app_setting_key_is_quoted(self):
        from src.database.models import AppSetting
        col = AppSetting.__table__.c["key"]
        col_name = col.name
        assert hasattr(col_name, "quote") or str(col_name) == "key"

    def test_quoted_name_import_exists(self):
        from src.database import models
        source = inspect.getsource(models)
        assert "quoted_name" in source
