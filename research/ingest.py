"""Import OHLCV data from CSV, JSON, Kraken, and Coinbase."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from research.schema import ASSETS, CANONICAL_COLUMNS, make_canonical

logger = logging.getLogger(__name__)


def from_csv(path: Path, asset: str | None = None, source: str = "csv") -> pd.DataFrame:
    df = pd.read_csv(path)
    col_map = _detect_column_mapping(df)
    df = df.rename(columns=col_map)

    if "asset" not in df.columns:
        if asset is None:
            raise ValueError("CSV has no 'asset' column and no asset was specified")
        df["asset"] = asset
    if "source" not in df.columns:
        df["source"] = source

    return make_canonical(df)


def from_json(path: Path, asset: str | None = None, source: str = "json") -> pd.DataFrame:
    with open(path) as f:
        data = json.load(f)

    if isinstance(data, dict) and "result" in data:
        return from_kraken_json(data, asset or "UNKNOWN", source="kraken")

    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], list):
            return _from_array_rows(data, asset, source)
        if isinstance(data[0], dict):
            df = pd.DataFrame(data)
            col_map = _detect_column_mapping(df)
            df = df.rename(columns=col_map)
            if "asset" not in df.columns:
                if asset is None:
                    raise ValueError("JSON has no 'asset' field and no asset was specified")
                df["asset"] = asset
            if "source" not in df.columns:
                df["source"] = source
            return make_canonical(df)

    raise ValueError(f"Unrecognized JSON format in {path}")


def from_kraken_json(data: dict, asset: str, source: str = "kraken") -> pd.DataFrame:
    result_keys = [k for k in data.get("result", {}) if k != "last"]
    if not result_keys:
        raise ValueError("No OHLCV data in Kraken response")

    rows = data["result"][result_keys[0]]
    records = []
    for row in rows:
        records.append({
            "asset": asset,
            "timestamp": datetime.fromtimestamp(int(row[0]), tz=timezone.utc),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
            "source": source,
        })
    return make_canonical(pd.DataFrame(records))


def from_coinbase_json(data: list, asset: str, source: str = "coinbase") -> pd.DataFrame:
    records = []
    for row in data:
        records.append({
            "asset": asset,
            "timestamp": datetime.fromtimestamp(int(row[0]), tz=timezone.utc),
            "open": float(row[3]),
            "high": float(row[2]),
            "low": float(row[1]),
            "close": float(row[4]),
            "volume": float(row[5]),
            "source": source,
        })
    return make_canonical(pd.DataFrame(records))


def _from_array_rows(data: list[list], asset: str | None, source: str) -> pd.DataFrame:
    if len(data[0]) >= 7:
        return from_kraken_json({"result": {"data": data}}, asset or "UNKNOWN", source)
    if len(data[0]) == 6:
        return from_coinbase_json(data, asset or "UNKNOWN", source)
    raise ValueError(f"Array rows have {len(data[0])} elements, expected 6 or 7+")


def _detect_column_mapping(df: pd.DataFrame) -> dict[str, str]:
    mapping = {}
    aliases = {
        "timestamp": ["timestamp", "time", "date", "datetime", "open_time", "Date", "Timestamp"],
        "open": ["open", "Open", "o"],
        "high": ["high", "High", "h"],
        "low": ["low", "Low", "l"],
        "close": ["close", "Close", "c"],
        "volume": ["volume", "Volume", "vol", "v"],
        "asset": ["asset", "symbol", "pair", "Asset", "Symbol"],
        "source": ["source", "provider", "exchange", "Source"],
    }
    for canonical, alts in aliases.items():
        for alt in alts:
            if alt in df.columns and canonical not in df.columns:
                mapping[alt] = canonical
                break
    return mapping
