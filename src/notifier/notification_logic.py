from __future__ import annotations

import logging
from datetime import datetime

import pytz

from src.config import settings
from src.strategy.engine import TradeSignal

logger = logging.getLogger(__name__)


class NotificationManager:
    def __init__(self):
        self.tz = pytz.timezone(settings.timezone)
        self.active_start = settings.active_hours_start
        self.active_end = settings.active_hours_end

    def is_active_hours(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now(self.tz)
        elif now.tzinfo is None:
            now = self.tz.localize(now)
        else:
            now = now.astimezone(self.tz)
        return self.active_start <= now.hour < self.active_end

    def should_send(
        self, signal: TradeSignal, now: datetime | None = None
    ) -> tuple[bool, str]:
        if signal.signal_type == "NO_TRADE":
            return False, "NO_TRADE signals are never sent"

        is_active = self.is_active_hours(now)

        if not is_active:
            if self._is_emergency(signal):
                return True, "Emergency signal during night hours"
            if signal.signal_type == "BUY":
                return False, "BUY signals suppressed during night hours (23:00-08:00)"
            if signal.priority == "CRITICAL":
                return True, "Critical signal during night hours"
            return False, "Non-emergency signal suppressed during night hours"

        notification_reasons = [
            signal.signal_type in ("BUY", "SELL", "REDUCE", "TAKE_PROFIT", "MOVE_TO_USD"),
            signal.priority == "CRITICAL",
        ]

        if any(notification_reasons):
            return True, "Actionable signal during active hours"

        return False, "Signal does not require notification"

    def _is_emergency(self, signal: TradeSignal) -> bool:
        if signal.priority == "CRITICAL":
            return True
        if signal.signal_type in ("SELL", "MOVE_TO_USD"):
            if signal.distance_to_loss < 20:
                return True
        if signal.signal_type == "SELL" and "stop" in signal.reason.lower():
            return True
        return False

    def get_morning_report_time(self) -> tuple[int, int]:
        return self.active_start, 0

    def get_evening_report_time(self) -> tuple[int, int]:
        return 22, 30
