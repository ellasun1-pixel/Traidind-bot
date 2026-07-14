import pytest
from datetime import datetime

import pytz

from src.notifier.notification_logic import NotificationManager
from src.strategy.engine import TradeSignal
from src.strategy.regime import MarketRegime


@pytest.fixture
def nm():
    return NotificationManager()


def _make_signal(signal_type="BUY", priority="MEDIUM", distance_to_loss=50.0, reason="test"):
    return TradeSignal(
        signal_type=signal_type,
        priority=priority,
        asset_symbol="BTC/USD",
        regime=MarketRegime.TREND,
        entry_price=50000.0,
        reason=reason,
        distance_to_loss=distance_to_loss,
    )


class TestActiveHours:
    def test_active_during_day(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        day_time = tz.localize(datetime(2024, 6, 15, 12, 0))
        assert nm.is_active_hours(day_time) is True

    def test_inactive_at_night(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        night_time = tz.localize(datetime(2024, 6, 15, 2, 0))
        assert nm.is_active_hours(night_time) is False

    def test_inactive_at_23(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        late = tz.localize(datetime(2024, 6, 15, 23, 0))
        assert nm.is_active_hours(late) is False

    def test_active_at_08(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        morning = tz.localize(datetime(2024, 6, 15, 8, 0))
        assert nm.is_active_hours(morning) is True


class TestNightMode:
    def test_buy_suppressed_at_night(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        night = tz.localize(datetime(2024, 6, 15, 1, 0))
        signal = _make_signal("BUY")
        should, reason = nm.should_send(signal, night)
        assert should is False
        assert "night" in reason.lower()

    def test_emergency_sell_at_night(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        night = tz.localize(datetime(2024, 6, 15, 1, 0))
        signal = _make_signal("SELL", "CRITICAL", distance_to_loss=10.0, reason="stop-loss hit")
        should, reason = nm.should_send(signal, night)
        assert should is True
        assert "emergency" in reason.lower() or "critical" in reason.lower()

    def test_critical_at_night(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        night = tz.localize(datetime(2024, 6, 15, 3, 0))
        signal = _make_signal("SELL", "CRITICAL")
        should, reason = nm.should_send(signal, night)
        assert should is True


class TestNoTradeNotification:
    def test_no_trade_never_sent(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        day = tz.localize(datetime(2024, 6, 15, 12, 0))
        signal = _make_signal("NO_TRADE")
        should, reason = nm.should_send(signal, day)
        assert should is False

    def test_no_trade_never_sent_at_night(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        night = tz.localize(datetime(2024, 6, 15, 2, 0))
        signal = _make_signal("NO_TRADE")
        should, reason = nm.should_send(signal, night)
        assert should is False


class TestDaySignals:
    def test_buy_sent_during_day(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        day = tz.localize(datetime(2024, 6, 15, 12, 0))
        signal = _make_signal("BUY")
        should, _ = nm.should_send(signal, day)
        assert should is True

    def test_sell_sent_during_day(self, nm):
        tz = pytz.timezone("Asia/Jerusalem")
        day = tz.localize(datetime(2024, 6, 15, 12, 0))
        signal = _make_signal("SELL")
        should, _ = nm.should_send(signal, day)
        assert should is True
