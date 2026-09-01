"""Tests for: calendar manual management, balance blocking, position alerts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

import pytest

from src.calendar.manager import CalendarManager, format_event_alert
from src.calendar.providers import MarketEvent, fetch_fomc_events
from src.database.models import CalendarEvent


class TestCalendarAddRemove:
    @pytest.fixture
    def manager(self):
        mgr = CalendarManager()
        mgr._initialized = True
        return mgr

    def test_add_event_creates_new(self, manager):
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = None
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            ev = manager.add_event(
                title="Test CPI",
                event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
            )

        mock_session.add.assert_called_once()
        added = mock_session.add.call_args[0][0]
        assert added.title == "Test CPI"
        assert added.source == "manual"
        assert added.category == "custom"
        assert "manual:" in added.unique_key

    def test_add_event_rejects_duplicate(self, manager):
        existing = MagicMock()
        existing.title = "Test CPI"
        existing.event_time = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = existing
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(ValueError, match="already exists"):
                manager.add_event(
                    title="Test CPI",
                    event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
                )

    def test_remove_event_success(self, manager):
        ev = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = ev

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            result = manager.remove_event(42)

        assert result is True
        mock_session.delete.assert_called_once_with(ev)

    def test_remove_event_not_found(self, manager):
        mock_session = MagicMock()
        mock_session.get.return_value = None

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            result = manager.remove_event(999)

        assert result is False

    def test_list_all_upcoming(self, manager):
        now = datetime.now(timezone.utc)
        ev1 = MagicMock(spec=CalendarEvent)
        ev1.event_time = now + timedelta(hours=2)
        ev2 = MagicMock(spec=CalendarEvent)
        ev2.event_time = now + timedelta(hours=5)

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.all.return_value = [ev1, ev2]
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            events = manager.list_all_upcoming()

        assert len(events) == 2


class TestCalendarAlertWindows:
    def test_alert_windows_are_18_and_1(self):
        from src.calendar.manager import ALERT_WINDOWS_HOURS, _ALERT_COL_MAP
        assert ALERT_WINDOWS_HOURS == [18, 1]
        assert _ALERT_COL_MAP[18] == "alerted_24h"
        assert _ALERT_COL_MAP[1] == "alerted_1h"

    def test_alert_18h_uses_alerted_24h_column(self):
        now = datetime.now(timezone.utc)
        ev = MagicMock(spec=CalendarEvent)
        ev.event_time = now + timedelta(hours=18)
        ev.alerted_24h = False
        ev.alerted_1h = False

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = [ev]
        mock_session.query.return_value = mock_query

        mgr = CalendarManager()
        mgr._initialized = True

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            alerts = mgr.get_events_needing_alert()

        assert len(alerts) >= 1


class TestFomcOnlyRefresh:
    def test_refresh_uses_fomc_not_external_apis(self):
        mgr = CalendarManager()
        mgr._initialized = True

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = None
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.fetch_fomc_events") as mock_fomc, \
             patch("src.calendar.manager.get_session") as mock_get_session:
            mock_fomc.return_value = []
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            stored = mgr.refresh_events()

        mock_fomc.assert_called_once_with(days_ahead=30)
        assert stored == 0

    def test_fetch_fomc_events_wrapper(self):
        events = fetch_fomc_events(days_ahead=365)
        for ev in events:
            assert ev.source == "fed_hardcoded"
            assert ev.impact == "high"


class TestBalanceBlocking:
    @pytest.mark.asyncio
    async def test_manual_buy_blocks_insufficient_balance(self):
        from src.telegram_bot.bot import cmd_manual_buy

        update = MagicMock()
        update.effective_user.id = 123456789
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["BTC", "50000"]

        mock_portfolio = MagicMock()
        mock_portfolio.balance_usd = 100.0

        with patch("src.telegram_bot.bot.get_portfolio", return_value=mock_portfolio), \
             patch("src.telegram_bot.bot.get_live_prices", new_callable=AsyncMock, return_value={"BTC/USD": 60000.0}), \
             patch("src.telegram_bot.bot.get_user_permissions") as mock_perms:
            mock_perms.return_value.can = MagicMock(return_value=True)

            await cmd_manual_buy(update, context)

        calls = update.message.reply_text.call_args_list
        blocked = any("Blocked" in str(c) or "insufficient" in str(c) for c in calls)
        assert blocked, f"Expected balance block message, got: {calls}"

    @pytest.mark.asyncio
    async def test_manual_buy_allows_sufficient_balance(self):
        from src.telegram_bot.bot import cmd_manual_buy

        update = MagicMock()
        update.effective_user.id = 123456789
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["BTC", "100"]

        mock_portfolio = MagicMock()
        mock_portfolio.balance_usd = 10000.0
        mock_portfolio.confirm_buy.return_value = (True, "Bought BTC")
        mock_portfolio.get_open_positions.return_value = []

        with patch("src.telegram_bot.bot.get_portfolio", return_value=mock_portfolio), \
             patch("src.telegram_bot.bot.get_live_prices", new_callable=AsyncMock, return_value={"BTC/USD": 60000.0}), \
             patch("src.telegram_bot.bot.get_user_permissions") as mock_perms, \
             patch("src.telegram_bot.bot.record_portfolio_snapshot"), \
             patch("src.telegram_bot.bot.get_session") as mock_gs:
            mock_perms.return_value.can = MagicMock(return_value=True)
            mock_gs.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            await cmd_manual_buy(update, context)

        calls = update.message.reply_text.call_args_list
        blocked = any("Blocked" in str(c) or "insufficient" in str(c) for c in calls)
        assert not blocked, f"Should not block, got: {calls}"


class TestPositionAlerts:
    @pytest.mark.asyncio
    async def test_stop_loss_alert_fires(self):
        from src.scheduler import jobs as jobs_mod
        from src.scheduler.jobs import _check_position_alerts

        mock_pos = MagicMock()
        mock_pos.status = "open"
        mock_pos.symbol = "BTC/USD"
        mock_pos.entry_price = 60000.0
        mock_pos.stop_loss = 58200.0
        mock_pos.peak_price = 62000.0

        mock_portfolio = MagicMock()
        mock_portfolio.positions = [mock_pos]

        mock_send = AsyncMock()
        original_send = jobs_mod._send_message_func
        original_sl = jobs_mod._alerted_stop_loss.copy()

        try:
            jobs_mod._send_message_func = mock_send
            jobs_mod._alerted_stop_loss.clear()

            with patch("src.scheduler.jobs.get_portfolio", return_value=mock_portfolio):
                await _check_position_alerts("BTC/USD", 58000.0)

            mock_send.assert_awaited_once()
            msg = mock_send.call_args[0][0]
            assert "STOP-LOSS" in msg
            assert "BTC/USD" in msg
        finally:
            jobs_mod._send_message_func = original_send
            jobs_mod._alerted_stop_loss = original_sl

    @pytest.mark.asyncio
    async def test_stop_loss_alert_fires_only_once(self):
        from src.scheduler import jobs as jobs_mod
        from src.scheduler.jobs import _check_position_alerts

        mock_pos = MagicMock()
        mock_pos.status = "open"
        mock_pos.symbol = "BTC/USD"
        mock_pos.entry_price = 60000.0
        mock_pos.stop_loss = 58200.0
        mock_pos.peak_price = 62000.0

        mock_portfolio = MagicMock()
        mock_portfolio.positions = [mock_pos]

        mock_send = AsyncMock()
        original_send = jobs_mod._send_message_func
        original_sl = jobs_mod._alerted_stop_loss.copy()

        try:
            jobs_mod._send_message_func = mock_send
            jobs_mod._alerted_stop_loss.clear()

            with patch("src.scheduler.jobs.get_portfolio", return_value=mock_portfolio):
                await _check_position_alerts("BTC/USD", 58000.0)
                await _check_position_alerts("BTC/USD", 57000.0)

            assert mock_send.await_count == 1
        finally:
            jobs_mod._send_message_func = original_send
            jobs_mod._alerted_stop_loss = original_sl

    @pytest.mark.asyncio
    async def test_drawdown_alert_fires(self):
        from src.scheduler import jobs as jobs_mod
        from src.scheduler.jobs import _check_position_alerts

        mock_pos = MagicMock()
        mock_pos.status = "open"
        mock_pos.symbol = "ETH/USD"
        mock_pos.entry_price = 3000.0
        mock_pos.stop_loss = 2910.0
        mock_pos.peak_price = 3500.0

        mock_portfolio = MagicMock()
        mock_portfolio.positions = [mock_pos]

        mock_send = AsyncMock()
        original_send = jobs_mod._send_message_func
        original_dd = jobs_mod._alerted_drawdown.copy()

        try:
            jobs_mod._send_message_func = mock_send
            jobs_mod._alerted_drawdown.clear()

            with patch("src.scheduler.jobs.get_portfolio", return_value=mock_portfolio):
                await _check_position_alerts("ETH/USD", 3100.0)

            mock_send.assert_awaited_once()
            msg = mock_send.call_args[0][0]
            assert "DRAWDOWN" in msg
            assert "ETH/USD" in msg
        finally:
            jobs_mod._send_message_func = original_send
            jobs_mod._alerted_drawdown = original_dd

    @pytest.mark.asyncio
    async def test_no_drawdown_if_peak_not_high_enough(self):
        from src.scheduler import jobs as jobs_mod
        from src.scheduler.jobs import _check_position_alerts

        mock_pos = MagicMock()
        mock_pos.status = "open"
        mock_pos.symbol = "ETH/USD"
        mock_pos.entry_price = 3000.0
        mock_pos.stop_loss = 2800.0
        mock_pos.peak_price = 3050.0

        mock_portfolio = MagicMock()
        mock_portfolio.positions = [mock_pos]

        mock_send = AsyncMock()
        original_send = jobs_mod._send_message_func
        original_dd = jobs_mod._alerted_drawdown.copy()
        original_sl = jobs_mod._alerted_stop_loss.copy()

        try:
            jobs_mod._send_message_func = mock_send
            jobs_mod._alerted_drawdown.clear()
            jobs_mod._alerted_stop_loss.clear()

            with patch("src.scheduler.jobs.get_portfolio", return_value=mock_portfolio):
                await _check_position_alerts("ETH/USD", 2850.0)

            mock_send.assert_not_awaited()
        finally:
            jobs_mod._send_message_func = original_send
            jobs_mod._alerted_drawdown = original_dd
            jobs_mod._alerted_stop_loss = original_sl
