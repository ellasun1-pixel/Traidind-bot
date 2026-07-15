from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from src.config import settings
from src.market_data.candle import Candle

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    valid: bool
    candle_count: int
    valid_candle_count: int
    invalid_candle_count: int
    oldest_candle: datetime | None
    newest_candle: datetime | None
    errors: list[str]
    warnings: list[str]


def validate_candles(
    candles: list[Candle],
    min_candles: int | None = None,
    max_age_hours: int | None = None,
) -> tuple[list[Candle], ValidationResult]:
    if min_candles is None:
        min_candles = settings.min_valid_candles
    if max_age_hours is None:
        max_age_hours = settings.max_daily_candle_age_hours

    errors: list[str] = []
    warnings: list[str] = []

    if not candles:
        return [], ValidationResult(
            valid=False, candle_count=0, valid_candle_count=0,
            invalid_candle_count=0, oldest_candle=None, newest_candle=None,
            errors=["No candles received"], warnings=[],
        )

    total_count = len(candles)

    valid_candles: list[Candle] = []
    for c in candles:
        ok, reason = c.is_valid()
        if ok:
            valid_candles.append(c)
        else:
            logger.debug("Invalid candle %s %s: %s", c.asset, c.open_time, reason)

    invalid_count = total_count - len(valid_candles)
    if invalid_count > 0:
        warnings.append(f"Removed {invalid_count} invalid candles")

    valid_candles.sort(key=lambda c: c.open_time)

    seen_times: set[datetime] = set()
    deduped: list[Candle] = []
    for c in valid_candles:
        if c.open_time in seen_times:
            warnings.append(f"Duplicate candle at {c.open_time}")
        else:
            seen_times.add(c.open_time)
            deduped.append(c)
    valid_candles = deduped

    if len(valid_candles) < min_candles:
        errors.append(
            f"Insufficient candles: {len(valid_candles)} valid, "
            f"{min_candles} required"
        )

    if valid_candles:
        newest = valid_candles[-1]
        age = datetime.now(timezone.utc) - newest.open_time
        if age > timedelta(hours=max_age_hours):
            hours = age.total_seconds() / 3600
            errors.append(
                f"Stale data: newest candle is {hours:.1f}h old "
                f"(max {max_age_hours}h)"
            )

    oldest_dt = valid_candles[0].open_time if valid_candles else None
    newest_dt = valid_candles[-1].open_time if valid_candles else None

    is_valid = len(errors) == 0

    return valid_candles, ValidationResult(
        valid=is_valid,
        candle_count=total_count,
        valid_candle_count=len(valid_candles),
        invalid_candle_count=invalid_count,
        oldest_candle=oldest_dt,
        newest_candle=newest_dt,
        errors=errors,
        warnings=warnings,
    )
