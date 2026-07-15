"""
Synthetic market data generator calibrated to real crypto asset characteristics.

Each asset gets distinct volatility, trend, and mean-reversion parameters
derived from published historical statistics for BTC, ETH, XRP, LINK, LTC.

Data is clearly labeled as SYNTHETIC throughout. No production conclusions
should be drawn from synthetic-only scenarios.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


ASSET_PROFILES = {
    "BTC/USD": {
        "base_price": 65000.0,
        "daily_vol": 0.025,
        "daily_drift": 0.0003,
        "mean_reversion": 0.02,
        "crash_prob": 0.008,
        "crash_magnitude": 0.12,
        "rally_prob": 0.005,
        "rally_magnitude": 0.10,
    },
    "ETH/USD": {
        "base_price": 3200.0,
        "daily_vol": 0.032,
        "daily_drift": 0.0002,
        "mean_reversion": 0.015,
        "crash_prob": 0.010,
        "crash_magnitude": 0.15,
        "rally_prob": 0.006,
        "rally_magnitude": 0.12,
    },
    "XRP/USD": {
        "base_price": 0.55,
        "daily_vol": 0.038,
        "daily_drift": 0.0001,
        "mean_reversion": 0.01,
        "crash_prob": 0.012,
        "crash_magnitude": 0.18,
        "rally_prob": 0.008,
        "rally_magnitude": 0.15,
    },
    "LINK/USD": {
        "base_price": 14.0,
        "daily_vol": 0.040,
        "daily_drift": 0.00015,
        "mean_reversion": 0.012,
        "crash_prob": 0.011,
        "crash_magnitude": 0.16,
        "rally_prob": 0.007,
        "rally_magnitude": 0.14,
    },
    "LTC/USD": {
        "base_price": 75.0,
        "daily_vol": 0.035,
        "daily_drift": 0.00005,
        "mean_reversion": 0.018,
        "crash_prob": 0.010,
        "crash_magnitude": 0.14,
        "rally_prob": 0.005,
        "rally_magnitude": 0.11,
    },
}


def generate_asset_data(
    symbol: str,
    n_days: int = 800,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate realistic synthetic daily OHLCV data for one asset."""
    profile = ASSET_PROFILES[symbol]
    rng = np.random.RandomState(seed)

    base = profile["base_price"]
    vol = profile["daily_vol"]
    drift = profile["daily_drift"]
    mr = profile["mean_reversion"]

    log_price = np.log(base)
    log_base = log_price
    closes = np.zeros(n_days)
    highs = np.zeros(n_days)
    lows = np.zeros(n_days)
    opens = np.zeros(n_days)
    volumes = np.zeros(n_days)

    vol_state = vol

    for i in range(n_days):
        vol_state = vol_state * 0.95 + vol * 0.05 + rng.normal(0, vol * 0.1)
        vol_state = max(vol * 0.3, min(vol * 3.0, vol_state))

        reversion = -mr * (log_price - log_base)
        daily_return = drift + reversion + rng.normal(0, vol_state)

        if rng.random() < profile["crash_prob"]:
            daily_return -= profile["crash_magnitude"] * rng.uniform(0.5, 1.0)
        if rng.random() < profile["rally_prob"]:
            daily_return += profile["rally_magnitude"] * rng.uniform(0.5, 1.0)

        open_price = np.exp(log_price)
        log_price += daily_return
        close_price = np.exp(log_price)

        intraday_vol = abs(daily_return) + vol_state * rng.uniform(0.2, 0.8)
        high_price = max(open_price, close_price) * (1 + rng.uniform(0.001, intraday_vol * 0.5))
        low_price = min(open_price, close_price) * (1 - rng.uniform(0.001, intraday_vol * 0.5))

        opens[i] = open_price
        closes[i] = close_price
        highs[i] = high_price
        lows[i] = low_price
        volumes[i] = rng.uniform(500, 5000) * (1 + abs(daily_return) * 20)

    dates = pd.date_range("2023-06-01", periods=n_days, freq="1D")
    return pd.DataFrame({
        "open_time": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def generate_all_assets(
    n_days: int = 800,
    base_seed: int = 42,
) -> dict[str, pd.DataFrame]:
    """Generate synthetic data for all 5 assets with correlated but distinct seeds."""
    return {
        symbol: generate_asset_data(symbol, n_days, seed=base_seed + i * 7)
        for i, symbol in enumerate(ASSET_PROFILES)
    }
