from __future__ import annotations

from enum import Enum

import pandas as pd


class MarketRegime(str, Enum):
    PANIC = "PANIC"
    LOWVOL = "LOWVOL"
    TREND = "TREND"
    CHOP = "CHOP"


def classify_regime(row: pd.Series) -> MarketRegime:
    price_change_48h = row.get("price_change_48h", 0) or 0
    rvol = row.get("rvol", 0) or 0
    rvol_median = row.get("rvol_median_252", 0) or 0
    rvol_pct25 = row.get("rvol_pct25", 0) or 0
    er20 = row.get("er20", 0) or 0
    close = row.get("close", 0)
    ema200 = row.get("ema200", 0) or 0
    ema50 = row.get("ema50", 0) or 0

    if price_change_48h <= -0.10 and rvol_median > 0 and rvol > 1.8 * rvol_median:
        return MarketRegime.PANIC

    if rvol_pct25 > 0 and rvol <= rvol_pct25 and er20 < 0.35:
        return MarketRegime.LOWVOL

    if er20 >= 0.35 and close > ema200 and ema50 > ema200:
        return MarketRegime.TREND

    return MarketRegime.CHOP
