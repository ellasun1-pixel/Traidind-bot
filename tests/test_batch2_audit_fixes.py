"""Tests for Batch 2 audit fixes.

Verifies:
(a) Silent $10K reset — PaperAccountRepository logs when creating new account
(b) _persist_sell logs when asset not found (no silent return)
(c) MODE_MISMATCH — positions from other modes are NOT adopted/stolen
"""
from __future__ import annotations

import inspect

import pytest


class TestSilentResetLogging:
    def test_get_or_create_logs_new_account(self):
        from src.database.repository import PaperAccountRepository
        source = inspect.getsource(PaperAccountRepository.get_or_create)
        assert "NO_ACCOUNT_FOUND" in source
        assert "logger.warning" in source

    def test_get_or_create_log_includes_balance(self):
        from src.database.repository import PaperAccountRepository
        source = inspect.getsource(PaperAccountRepository.get_or_create)
        assert "starting_balance" in source
        assert "default balance" in source or "balance=$" in source


class TestPersistSellLogging:
    def test_persist_sell_logs_missing_asset(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_sell)
        assert "logger.warning" in source
        assert "not found" in source.lower()

    def test_persist_buy_also_logs_missing_asset(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_buy)
        assert "logger.warning" in source
        assert "not found" in source.lower()


class TestModeMismatchNoStealing:
    def test_open_positions_not_adopted(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.restore_from_db)
        mismatch_region = source[source.index("MODE_MISMATCH"):]
        first_mismatch = mismatch_region[:mismatch_region.index("\n\n")]
        assert "NOT adopting" in first_mismatch
        assert "op.agent_mode = mode" not in first_mismatch

    def test_closed_positions_not_adopted(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.restore_from_db)
        second_idx = source.index("MODE_MISMATCH", source.index("MODE_MISMATCH") + 1)
        second_region = source[second_idx:]
        first_block = second_region[:second_region.index("\n\n") if "\n\n" in second_region else len(second_region)]
        assert "NOT adopting" in first_block
        assert "cp.agent_mode = mode" not in first_block

    def test_no_session_flush_after_mismatch(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.restore_from_db)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            if "MODE_MISMATCH" in line:
                next_lines = "\n".join(lines[i+1:i+6])
                assert "session.flush()" not in next_lines, \
                    f"session.flush() found near MODE_MISMATCH at line {i}: should not adopt"
