from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
import pandas as pd

from src.config import settings, AssetConfig

logger = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINBASE_BASE = "https://api.exchange.coinbase.com"

TIMEFRAME_MAP_KRAKEN = {"1d": 1440, "4h": 240, "1h": 60}
TIMEFRAME_MAP_COINBASE = {"1d": 86400, "4h": 14400, "1h": 3600}


class MarketDataFetcher:
    def __init__(self):
        self._cache: dict[str, tuple[datetime, pd.DataFrame]] = {}
        self._cache_ttl = timedelta(minutes=5)

    async def fetch_kraken_ohlc(
        self, asset: AssetConfig, timeframe: str = "1d", since_hours: int = 720
    ) -> pd.DataFrame:
        interval = TIMEFRAME_MAP_KRAKEN.get(timeframe, 1440)
        since = int((datetime.now(timezone.utc) - timedelta(hours=since_hours)).timestamp())
        url = f"{KRAKEN_BASE}/OHLC"
        params = {"pair": asset.kraken_pair, "interval": interval, "since": since}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if data.get("error") and len(data["error"]) > 0:
            raise ValueError(f"Kraken API error: {data['error']}")
        result_key = [k for k in data.get("result", {}) if k != "last"]
        if not result_key:
            return pd.DataFrame()
        rows = data["result"][result_key[0]]
        df = pd.DataFrame(rows, columns=[
            "time", "open", "high", "low", "close", "vwap", "volume", "count"
        ])
        df["time"] = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df.rename(columns={"time": "open_time"}, inplace=True)
        return df

    async def fetch_coinbase_ohlc(
        self, asset: AssetConfig, timeframe: str = "1d", since_hours: int = 720
    ) -> pd.DataFrame:
        granularity = TIMEFRAME_MAP_COINBASE.get(timeframe, 86400)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=since_hours)
        max_candles = 300
        all_rows = []
        current_start = start
        while current_start < end:
            chunk_end = min(current_start + timedelta(seconds=granularity * max_candles), end)
            url = f"{COINBASE_BASE}/products/{asset.coinbase_pair}/candles"
            params = {
                "start": current_start.isoformat(),
                "end": chunk_end.isoformat(),
                "granularity": granularity,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                rows = resp.json()
            all_rows.extend(rows)
            current_start = chunk_end
        if not all_rows:
            return pd.DataFrame()
        df = pd.DataFrame(all_rows, columns=["time", "low", "high", "open", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"].astype(int), unit="s", utc=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df.rename(columns={"time": "open_time"}, inplace=True)
        df.sort_values("open_time", inplace=True)
        df.drop_duplicates(subset=["open_time"], keep="last", inplace=True)
        return df

    async def fetch_ohlc(
        self, asset: AssetConfig, timeframe: str = "1d", since_hours: int = 720
    ) -> tuple[pd.DataFrame, str]:
        cache_key = f"{asset.symbol}_{timeframe}"
        if cache_key in self._cache:
            cached_time, cached_df = self._cache[cache_key]
            if datetime.now(timezone.utc) - cached_time < self._cache_ttl:
                return cached_df, "cache"
        try:
            df = await self.fetch_kraken_ohlc(asset, timeframe, since_hours)
            if not df.empty:
                self._cache[cache_key] = (datetime.now(timezone.utc), df)
                return df, "kraken"
        except Exception as e:
            logger.warning("Kraken fetch failed for %s: %s", asset.symbol, e)
        try:
            df = await self.fetch_coinbase_ohlc(asset, timeframe, since_hours)
            if not df.empty:
                self._cache[cache_key] = (datetime.now(timezone.utc), df)
                return df, "coinbase"
        except Exception as e:
            logger.warning("Coinbase fetch failed for %s: %s", asset.symbol, e)
        return pd.DataFrame(), "none"

    async def get_latest_price(self, asset: AssetConfig) -> Optional[float]:
        try:
            url = f"{KRAKEN_BASE}/Ticker"
            params = {"pair": asset.kraken_pair}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            if data.get("error") and len(data["error"]) > 0:
                raise ValueError(str(data["error"]))
            result_key = list(data["result"].keys())[0]
            return float(data["result"][result_key]["c"][0])
        except Exception as e:
            logger.warning("Kraken ticker failed for %s: %s", asset.symbol, e)
        try:
            url = f"{COINBASE_BASE}/products/{asset.coinbase_pair}/ticker"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            return float(data["price"])
        except Exception as e:
            logger.warning("Coinbase ticker failed for %s: %s", asset.symbol, e)
        return None

    async def check_source_divergence(
        self, asset: AssetConfig, threshold_pct: float | None = None
    ) -> tuple[bool, float]:
        if threshold_pct is None:
            threshold_pct = settings.divergence_threshold_pct
        kraken_price = None
        coinbase_price = None
        try:
            url = f"{KRAKEN_BASE}/Ticker"
            params = {"pair": asset.kraken_pair}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
            if not (data.get("error") and len(data["error"]) > 0):
                result_key = list(data["result"].keys())[0]
                kraken_price = float(data["result"][result_key]["c"][0])
        except Exception:
            pass
        try:
            url = f"{COINBASE_BASE}/products/{asset.coinbase_pair}/ticker"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            coinbase_price = float(data["price"])
        except Exception:
            pass
        if kraken_price is None or coinbase_price is None:
            return True, 0.0
        mid = (kraken_price + coinbase_price) / 2
        divergence = abs(kraken_price - coinbase_price) / mid
        return divergence > threshold_pct, divergence
