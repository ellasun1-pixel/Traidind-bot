"""Canonical OHLCV schema for all research data."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

CANONICAL_COLUMNS = ["asset", "timestamp", "open", "high", "low", "close", "volume", "source"]
OHLCV_DTYPES = {"open": float, "high": float, "low": float, "close": float, "volume": float}

ASSETS = {
    "BTC/USD": {"kraken_pair": "XXBTZUSD", "coinbase_pair": "BTC-USD"},
    "ETH/USD": {"kraken_pair": "XETHZUSD", "coinbase_pair": "ETH-USD"},
    "XRP/USD": {"kraken_pair": "XXRPZUSD", "coinbase_pair": "XRP-USD"},
    "LINK/USD": {"kraken_pair": "LINKUSD", "coinbase_pair": "LINK-USD"},
    "LTC/USD": {"kraken_pair": "XLTCZUSD", "coinbase_pair": "LTC-USD"},
}


def make_canonical(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    df = df[CANONICAL_COLUMNS].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col, dtype in OHLCV_DTYPES.items():
        df[col] = df[col].astype(dtype)
    df["asset"] = df["asset"].astype(str)
    df["source"] = df["source"].astype(str)
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df.drop_duplicates(subset=["asset", "timestamp"], keep="last").reset_index(drop=True)
    return df


def to_engine_df(df: pd.DataFrame) -> pd.DataFrame:
    """Convert canonical OHLCV to the DataFrame format StrategyEngine expects."""
    engine_df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    engine_df = engine_df.rename(columns={"timestamp": "open_time"})
    engine_df = engine_df.sort_values("open_time").reset_index(drop=True)
    return engine_df


def save_data(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        df_out = df.copy()
        df_out["timestamp"] = df_out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        df_out.to_json(path, orient="records", indent=2)
    else:
        df_out = df.copy()
        df_out["timestamp"] = df_out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        df_out.to_csv(path, index=False)


def load_data(path: Path) -> pd.DataFrame:
    if path.suffix == ".json":
        df = pd.read_json(path)
    else:
        df = pd.read_csv(path)
    return make_canonical(df)
