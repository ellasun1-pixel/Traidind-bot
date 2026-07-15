from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Candle:
    asset: str
    timeframe: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    fetched_at: datetime

    def is_valid(self) -> tuple[bool, str]:
        if self.open_time is None:
            return False, "missing open_time"
        for field_name in ("open", "high", "low", "close"):
            val = getattr(self, field_name)
            if val is None:
                return False, f"missing {field_name}"
            if math.isnan(val) or math.isinf(val):
                return False, f"{field_name} is NaN/Inf"
        if self.close <= 0:
            return False, f"close price {self.close} <= 0"
        if self.open <= 0:
            return False, f"open price {self.open} <= 0"
        if self.high < self.low:
            return False, f"high {self.high} < low {self.low}"
        if self.open < self.low or self.open > self.high:
            return False, f"open {self.open} outside low-high range [{self.low}, {self.high}]"
        if self.close < self.low or self.close > self.high:
            return False, f"close {self.close} outside low-high range [{self.low}, {self.high}]"
        if self.volume is None:
            return False, "missing volume"
        if math.isnan(self.volume) or math.isinf(self.volume):
            return False, "volume is NaN/Inf"
        if self.open_time.tzinfo is None:
            return False, "open_time is not timezone-aware"
        now = datetime.now(timezone.utc)
        if self.open_time > now:
            return False, f"open_time {self.open_time} is in the future"
        return True, ""


@dataclass
class PriceQuote:
    asset: str
    price: float
    source: str
    fetched_at: datetime
