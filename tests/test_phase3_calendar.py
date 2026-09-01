"""Tests for Phase 3: market events calendar."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from src.calendar.providers import (
    MarketEvent,
    _build_crypto_map,
    _is_high_impact,
    get_fomc_events,
    fetch_all_events,
    init_crypto_map,
)
from src.calendar.manager import (
    CalendarManager,
    format_event_alert,
    format_calendar_view,
)
from src.database.models import CalendarEvent


class TestMarketEvent:
    def test_unique_key_format(self):
        ev = MarketEvent(
            title="CPI Release",
            event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
        )
        assert ev.unique_key == "jblanked:20260910:CPI Release"

    def test_unique_key_truncates_long_title(self):
        ev = MarketEvent(
            title="A" * 100,
            event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
        )
        key = ev.unique_key
        title_part = key.split(":", 2)[2]
        assert len(title_part) == 60


class TestCryptoMap:
    def test_build_crypto_map_basic(self):
        m = _build_crypto_map(["BTC/USD", "ETH/USD"])
        assert "btc" in m
        assert "bitcoin" in m
        assert m["btc"] == ["BTC/USD"]
        assert "eth" in m
        assert "ethereum" in m

    def test_build_crypto_map_unknown_coin(self):
        m = _build_crypto_map(["XYZ/USD"])
        assert "xyz" in m
        assert m["xyz"] == ["XYZ/USD"]


class TestHighImpact:
    def test_fomc_keyword(self):
        assert _is_high_impact("FOMC Interest Rate Decision") is True

    def test_cpi_keyword(self):
        assert _is_high_impact("Consumer Price Index") is True

    def test_unrelated(self):
        assert _is_high_impact("Some random event") is False


class TestFomcEvents:
    def test_returns_future_events_only(self):
        with patch("src.calendar.providers.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 1, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            events = get_fomc_events(days_ahead=60)
            assert len(events) > 0
            for ev in events:
                assert ev.event_time >= datetime(2026, 7, 1, tzinfo=timezone.utc)
                assert ev.source == "fed_hardcoded"
                assert ev.impact == "high"
                assert ev.category == "macro"

    def test_no_events_when_far_in_past(self):
        with patch("src.calendar.providers.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2028, 1, 1, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            events = get_fomc_events(days_ahead=30)
            assert events == []


class TestFormatEventAlert:
    def test_basic_format(self):
        ev = CalendarEvent(
            title="CPI Release",
            event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
            impact="high",
            currency="USD",
        )
        text = format_event_alert(ev, 24)
        assert "Market Event Alert" in text
        assert "24h before" in text
        assert "CPI Release" in text
        assert "Sep 10" in text

    def test_crypto_event_with_asset(self):
        ev = CalendarEvent(
            title="Hard Fork",
            event_time=datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc),
            category="crypto",
            source="coinmarketcal",
            impact="high",
            asset_symbol="ETH/USD",
        )
        text = format_event_alert(ev, 1)
        assert "1h before" in text
        assert "ETH/USD" in text

    def test_forecast_and_previous_shown(self):
        ev = CalendarEvent(
            title="NFP",
            event_time=datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
            impact="high",
            forecast="180K",
            previous="150K",
        )
        text = format_event_alert(ev, 24)
        assert "180K" in text
        assert "150K" in text


class TestFormatCalendarView:
    def test_empty_events(self):
        text = format_calendar_view([])
        assert "No upcoming events" in text

    def test_events_grouped_by_date(self):
        ev1 = CalendarEvent(
            title="Event A",
            event_time=datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
            impact="high",
        )
        ev2 = CalendarEvent(
            title="Event B",
            event_time=datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc),
            category="crypto",
            source="coinmarketcal",
            impact="medium",
        )
        ev3 = CalendarEvent(
            title="Event C",
            event_time=datetime(2026, 9, 11, 8, 0, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
            impact="low",
        )
        text = format_calendar_view([ev1, ev2, ev3])
        assert "Next 48h" in text
        assert "[MACRO]" in text
        assert "[CRYPTO]" in text
        assert "Total: 3 events" in text


class TestCalendarManagerRefresh:
    @pytest.fixture
    def manager(self):
        mgr = CalendarManager()
        mgr._initialized = True
        return mgr

    def test_refresh_stores_new_events(self, manager):
        now = datetime.now(timezone.utc)
        mock_events = [
            MarketEvent(
                title="Test Event",
                event_time=now + timedelta(days=1),
                category="macro",
                source="fed_hardcoded",
                impact="high",
            )
        ]

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = None
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.fetch_fomc_events", return_value=mock_events), \
             patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            stored = manager.refresh_events()

        assert stored == 1
        mock_session.add.assert_called_once()

    def test_refresh_updates_existing_events(self, manager):
        now = datetime.now(timezone.utc)
        mock_events = [
            MarketEvent(
                title="Test Event",
                event_time=now + timedelta(days=1),
                category="macro",
                source="fed_hardcoded",
                impact="high",
                description="Updated desc",
            )
        ]

        existing = MagicMock()
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.first.return_value = existing
        mock_session.query.return_value = mock_query

        with patch("src.calendar.manager.fetch_fomc_events", return_value=mock_events), \
             patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            stored = manager.refresh_events()

        assert stored == 0
        assert existing.description == "Updated desc"
        assert existing.impact == "high"


class TestCalendarManagerAlerts:
    def test_get_events_needing_alert(self):
        now = datetime.now(timezone.utc)

        ev_24h = MagicMock(spec=CalendarEvent)
        ev_24h.alerted_24h = False
        ev_24h.alerted_1h = False
        ev_24h.event_time = now + timedelta(hours=24)

        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = [ev_24h]
        mock_session.query.return_value = mock_query

        mgr = CalendarManager()
        mgr._initialized = True

        with patch("src.calendar.manager.get_session") as mock_get_session:
            mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

            alerts = mgr.get_events_needing_alert()

        assert len(alerts) >= 1


class TestCalendarModel:
    def test_calendar_event_model_fields(self):
        ev = CalendarEvent(
            unique_key="test:20260910:CPI",
            title="CPI Release",
            event_time=datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc),
            category="macro",
            source="jblanked",
            impact="high",
            alerted_24h=False,
            alerted_1h=False,
        )
        assert ev.unique_key == "test:20260910:CPI"
        assert ev.alerted_24h is False
        assert ev.alerted_1h is False


class TestMigration014:
    def _load_migration(self):
        import importlib
        import sys
        mod_name = "alembic_014_phase3"
        if mod_name not in sys.modules:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                mod_name,
                "/home/user/Traidind-bot/alembic/versions/014_phase3_calendar_events.py",
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        return sys.modules[mod_name]

    def test_migration_metadata(self):
        mod = self._load_migration()
        assert mod.revision == "014"
        assert mod.down_revision == "013"

    def test_upgrade_creates_table(self):
        mod = self._load_migration()
        with patch.object(mod, "op") as mock_op:
            mod.upgrade()
            mock_op.create_table.assert_called_once()
            call_args = mock_op.create_table.call_args
            assert call_args[0][0] == "calendar_events"
            mock_op.create_index.assert_called_once_with(
                "ix_calendar_event_time", "calendar_events", ["event_time"]
            )

    def test_downgrade_drops_table(self):
        mod = self._load_migration()
        with patch.object(mod, "op") as mock_op:
            mod.downgrade()
            mock_op.drop_index.assert_called_once()
            mock_op.drop_table.assert_called_once_with("calendar_events")


class TestSchedulerCalendarJobs:
    @pytest.mark.asyncio
    async def test_calendar_refresh_job_calls_manager(self):
        from src.scheduler.jobs import calendar_refresh_job

        mock_manager = MagicMock()
        mock_manager.refresh_events.return_value = 5

        with patch("src.calendar.manager.get_calendar_manager", return_value=mock_manager):
            await calendar_refresh_job()

        mock_manager.refresh_events.assert_called_once()

    @pytest.mark.asyncio
    async def test_calendar_alert_job_sends_alerts(self):
        from src.scheduler import jobs as jobs_mod
        from src.scheduler.jobs import calendar_alert_job

        mock_event = MagicMock(spec=CalendarEvent)
        mock_event.title = "CPI"
        mock_event.event_time = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)
        mock_event.category = "macro"
        mock_event.source = "jblanked"
        mock_event.impact = "high"
        mock_event.asset_symbol = None
        mock_event.description = ""
        mock_event.forecast = ""
        mock_event.previous = ""
        mock_event.currency = "USD"

        mock_manager = MagicMock()
        mock_manager.get_events_needing_alert.return_value = [(mock_event, 24)]

        mock_send = AsyncMock()
        original = jobs_mod._send_message_func

        with patch("src.calendar.manager.get_calendar_manager", return_value=mock_manager):
            jobs_mod._send_message_func = mock_send
            try:
                await calendar_alert_job()
            finally:
                jobs_mod._send_message_func = original

        mock_send.assert_awaited_once()
