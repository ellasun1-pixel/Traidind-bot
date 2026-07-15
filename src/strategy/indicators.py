from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def efficiency_ratio(series: pd.Series, period: int = 20) -> pd.Series:
    direction = (series - series.shift(period)).abs()
    volatility = series.diff().abs().rolling(period).sum()
    return (direction / volatility).replace([np.inf, -np.inf], 0).fillna(0)


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di)).replace(
        [np.inf, -np.inf], 0
    ).fillna(0)
    adx_val = dx.ewm(span=period, adjust=False).mean()
    return adx_val


def realized_volatility(close: pd.Series, window: int = 20) -> pd.Series:
    log_returns = np.log(close / close.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(252)


WARMUP = {
    "ema50": 50,
    "ema200": 200,
    "er20": 20,
    "adx14": 28,
    "rvol": 20,
    "rvol_median_60": 80,
    "rvol_pct25_60": 80,
    "price_change_48h": 2,
    "price_change_short": 1,
}

MIN_CANDLES_FOR_FULL_VALIDITY = 200


def indicator_warmup_status(n_candles: int) -> dict[str, bool]:
    return {name: n_candles >= req for name, req in WARMUP.items()}


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["er20"] = efficiency_ratio(df["close"], 20)

    if all(col in df.columns for col in ["high", "low"]):
        df["adx14"] = adx(df["high"], df["low"], df["close"], 14)
        df["has_ohlc"] = True
    else:
        df["adx14"] = np.nan
        df["has_ohlc"] = False

    df["rvol"] = realized_volatility(df["close"], 20)
    df["rvol_median_252"] = df["rvol"].rolling(60).median()
    df["rvol_pct25"] = df["rvol"].rolling(60).quantile(0.25)
    df["price_change_48h"] = df["close"].pct_change(periods=2)
    df["price_change_short"] = df["close"].pct_change(periods=1)
    return df
