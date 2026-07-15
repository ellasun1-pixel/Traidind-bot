from __future__ import annotations

from enum import Enum

import numpy as np
import pandas as pd


class MarketRegime(str, Enum):
    PANIC = "PANIC"
    LOWVOL = "LOWVOL"
    TREND = "TREND"
    CHOP = "CHOP"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"


_REGIME_FIELDS = ["er20", "ema50", "ema200", "rvol", "rvol_median_252", "rvol_pct25", "price_change_48h"]


def _is_nan(val) -> bool:
    if val is None:
        return True
    try:
        return np.isnan(float(val))
    except (TypeError, ValueError):
        return True


def regime_nan_fields(row: pd.Series) -> list[str]:
    return [f for f in _REGIME_FIELDS if _is_nan(row.get(f))]


def classify_regime(row: pd.Series) -> MarketRegime:
    nan_fields = regime_nan_fields(row)
    if nan_fields:
        return MarketRegime.DATA_INSUFFICIENT

    price_change_48h = float(row["price_change_48h"])
    rvol = float(row["rvol"])
    rvol_median = float(row["rvol_median_252"])
    rvol_pct25 = float(row["rvol_pct25"])
    er20 = float(row["er20"])
    close = float(row.get("close", 0))
    ema200 = float(row["ema200"])
    ema50 = float(row["ema50"])

    if price_change_48h <= -0.10 and rvol_median > 0 and rvol > 1.8 * rvol_median:
        return MarketRegime.PANIC

    if rvol_pct25 > 0 and rvol <= rvol_pct25 and er20 < 0.35:
        return MarketRegime.LOWVOL

    if er20 >= 0.35 and close > ema200 and ema50 > ema200:
        return MarketRegime.TREND

    return MarketRegime.CHOP
