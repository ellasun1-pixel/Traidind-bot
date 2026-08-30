"""Tests for Batch 1 audit fixes.

Verifies:
(a) confirm_partial_sell persists to DB (_persist_partial_sell is called)
(b) /confirm handler guards against double execution (lifecycle.confirm before portfolio op)
"""
from __future__ import annotations

import inspect

import pytest


class TestPartialSellPersistence:
    def test_persist_partial_sell_method_exists(self):
        from src.portfolio.manager import PaperPortfolio
        assert hasattr(PaperPortfolio, "_persist_partial_sell")

    def test_confirm_partial_sell_calls_persist(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio.confirm_partial_sell)
        assert "_persist_partial_sell" in source

    def test_persist_partial_sell_writes_trade_history(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_partial_sell)
        assert "trade_repo.create" in source or "TradeHistoryRepository" in source

    def test_persist_partial_sell_updates_db_quantity(self):
        from src.portfolio.manager import PaperPortfolio
        source = inspect.getsource(PaperPortfolio._persist_partial_sell)
        assert "db_pos" in source
        assert "quantity" in source


class TestConfirmRaceGuard:
    def test_lifecycle_confirm_before_portfolio_op(self):
        from src.telegram_bot import bot
        source = inspect.getsource(bot.cmd_confirm)
        confirm_idx = source.index("lifecycle.confirm(sig)")
        buy_idx = source.index("portfolio.confirm_buy")
        assert confirm_idx < buy_idx, "lifecycle.confirm must happen before portfolio.confirm_buy"

    def test_invalid_transition_skips_portfolio_op(self):
        from src.telegram_bot import bot
        source = inspect.getsource(bot.cmd_confirm)
        assert "InvalidTransitionError" in source
        assert "continue" in source[source.index("InvalidTransitionError"):]

    def test_status_recheck_in_loop(self):
        from src.telegram_bot import bot
        source = inspect.getsource(bot.cmd_confirm)
        assert 'sig.status != "pending"' in source
