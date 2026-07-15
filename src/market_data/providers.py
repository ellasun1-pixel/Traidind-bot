from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta

import httpx

from src.market_data.candle import Candle, PriceQuote

logger = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINBASE_BASE = "https://api.exchange.coinbase.com"

TIMEFRAME_INTERVAL_KRAKEN = {"1d": 1440, "4h": 240, "1h": 60}
TIMEFRAME_INTERVAL_COINBASE = {"1d": 86400, "4h": 14400, "1h": 3600}


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    async def fetch_ohlcv(
        self, symbol: str, pair: str, timeframe: str, count: int
    ) -> list[Candle]:
        ...

    @abstractmethod
    async def get_current_price(self, symbol: str, pair: str) -> PriceQuote | None:
        ...

    @abstractmethod
    def validate_pair(self, pair: str) -> bool:
        ...


class KrakenAdapter(ProviderAdapter):
    name = "kraken"

    async def fetch_ohlcv(
        self, symbol: str, pair: str, timeframe: str, count: int
    ) -> list[Candle]:
        interval = TIMEFRAME_INTERVAL_KRAKEN.get(timeframe, 1440)
        hours_needed = int(count * (interval / 60))
        since = int(
            (datetime.now(timezone.utc) - timedelta(hours=hours_needed)).timestamp()
        )
        now = datetime.now(timezone.utc)

        url = f"{KRAKEN_BASE}/OHLC"
        params = {"pair": pair, "interval": interval, "since": since}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("error") and len(data["error"]) > 0:
            raise ValueError(f"Kraken API error: {data['error']}")

        result_keys = [k for k in data.get("result", {}) if k != "last"]
        if not result_keys:
            return []

        rows = data["result"][result_keys[0]]
        candles = []
        for row in rows:
            try:
                open_time = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
                candles.append(Candle(
                    asset=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                    source=self.name,
                    fetched_at=now,
                ))
            except (ValueError, IndexError, TypeError) as e:
                logger.debug("Kraken candle parse error for %s: %s", symbol, e)

        return candles

    async def get_current_price(self, symbol: str, pair: str) -> PriceQuote | None:
        url = f"{KRAKEN_BASE}/Ticker"
        params = {"pair": pair}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        if data.get("error") and len(data["error"]) > 0:
            raise ValueError(f"Kraken ticker error: {data['error']}")

        result_key = list(data["result"].keys())[0]
        price = float(data["result"][result_key]["c"][0])
        return PriceQuote(
            asset=symbol, price=price,
            source=self.name, fetched_at=datetime.now(timezone.utc),
        )

    def validate_pair(self, pair: str) -> bool:
        return bool(pair and len(pair) >= 3)


class CoinbaseAdapter(ProviderAdapter):
    name = "coinbase"

    async def fetch_ohlcv(
        self, symbol: str, pair: str, timeframe: str, count: int
    ) -> list[Candle]:
        granularity = TIMEFRAME_INTERVAL_COINBASE.get(timeframe, 86400)
        end = datetime.now(timezone.utc)
        hours_needed = int(count * (granularity / 3600))
        start = end - timedelta(hours=hours_needed)
        now = end

        max_per_request = 300
        all_candles: list[Candle] = []
        current_start = start

        while current_start < end:
            chunk_end = min(
                current_start + timedelta(seconds=granularity * max_per_request),
                end,
            )
            url = f"{COINBASE_BASE}/products/{pair}/candles"
            params = {
                "start": current_start.isoformat(),
                "end": chunk_end.isoformat(),
                "granularity": granularity,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                rows = resp.json()

            if not isinstance(rows, list):
                raise ValueError(f"Coinbase returned non-list: {type(rows)}")

            for row in rows:
                try:
                    open_time = datetime.fromtimestamp(int(row[0]), tz=timezone.utc)
                    # Coinbase order: [time, low, high, open, close, volume]
                    all_candles.append(Candle(
                        asset=symbol,
                        timeframe=timeframe,
                        open_time=open_time,
                        open=float(row[3]),
                        high=float(row[2]),
                        low=float(row[1]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        source=self.name,
                        fetched_at=now,
                    ))
                except (ValueError, IndexError, TypeError) as e:
                    logger.debug("Coinbase candle parse error for %s: %s", symbol, e)

            current_start = chunk_end

        return all_candles

    async def get_current_price(self, symbol: str, pair: str) -> PriceQuote | None:
        url = f"{COINBASE_BASE}/products/{pair}/ticker"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        price = float(data["price"])
        return PriceQuote(
            asset=symbol, price=price,
            source=self.name, fetched_at=datetime.now(timezone.utc),
        )

    def validate_pair(self, pair: str) -> bool:
        return bool(pair and "-" in pair)
