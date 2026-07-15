"""Fetch historical daily OHLCV from Kraken and Coinbase.

Usage:
    python -m research.fetch_data [--provider kraken|coinbase] [--assets BTC/USD,ETH/USD] [--days 730]

Provider limits:
    - Kraken returns max ~720 candles per OHLC request. This fetcher
      paginates using the ``last`` cursor to retrieve longer histories.
    - Coinbase returns max 300 candles per request. This fetcher
      paginates with time-windowed chunks.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import pandas as pd

from research.schema import ASSETS, make_canonical, save_data

logger = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINBASE_BASE = "https://api.exchange.coinbase.com"
KRAKEN_MAX_CANDLES_PER_REQUEST = 720

DATA_DIR = Path(__file__).parent / "data"


def _exclude_incomplete_candle(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the current (incomplete) daily candle.

    A daily candle whose timestamp is today in UTC is still forming and
    would have incorrect OHLCV values.  Drop it so only completed
    candles enter the backtest.
    """
    if df.empty:
        return df
    today = pd.Timestamp.now(tz="UTC").normalize()
    return df[df["timestamp"] < today].copy()


def _deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """Drop duplicate rows by (asset, timestamp), keeping the last occurrence."""
    if df.empty:
        return df
    return df.drop_duplicates(subset=["asset", "timestamp"], keep="last").reset_index(drop=True)


async def fetch_kraken(asset: str, days: int) -> pd.DataFrame:
    """Fetch daily candles from Kraken with pagination.

    Kraken's OHLC endpoint returns at most ~720 candles and a ``last``
    timestamp that serves as a cursor for the next page.  We loop until
    all requested history is retrieved.
    """
    pair = ASSETS[asset]["kraken_pair"]
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    all_records: list[dict] = []
    seen_timestamps: set[int] = set()

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{KRAKEN_BASE}/OHLC",
                params={"pair": pair, "interval": 1440, "since": since},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("error") and len(data["error"]) > 0:
                raise ValueError(f"Kraken error for {asset}: {data['error']}")

            result_keys = [k for k in data.get("result", {}) if k != "last"]
            if not result_keys:
                break

            rows = data["result"][result_keys[0]]
            if not rows:
                break

            new_count = 0
            for row in rows:
                ts = int(row[0])
                if ts in seen_timestamps:
                    continue
                seen_timestamps.add(ts)
                all_records.append({
                    "asset": asset,
                    "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[6]),
                    "source": "kraken",
                })
                new_count += 1

            last_cursor = data.get("result", {}).get("last")
            if last_cursor is None or new_count == 0:
                break
            if len(rows) < KRAKEN_MAX_CANDLES_PER_REQUEST:
                break

            since = int(last_cursor)

    if not all_records:
        raise ValueError(f"No data returned for {asset} from Kraken")

    df = make_canonical(pd.DataFrame(all_records))
    df = _exclude_incomplete_candle(df)
    df = _deduplicate(df)
    return df


async def fetch_coinbase(asset: str, days: int) -> pd.DataFrame:
    """Fetch daily candles from Coinbase with pagination.

    Coinbase returns max 300 candles per request.  We paginate with
    non-overlapping time windows and deduplicate any boundary candles.
    """
    pair = ASSETS[asset]["coinbase_pair"]
    granularity = 86400
    max_per_request = 300
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    all_records: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        current_start = start
        while current_start < end:
            chunk_end = min(
                current_start + timedelta(seconds=granularity * max_per_request),
                end,
            )
            resp = await client.get(
                f"{COINBASE_BASE}/products/{pair}/candles",
                params={
                    "start": current_start.isoformat(),
                    "end": chunk_end.isoformat(),
                    "granularity": granularity,
                },
            )
            resp.raise_for_status()
            rows = resp.json()

            if not isinstance(rows, list):
                raise ValueError(f"Coinbase returned non-list for {asset}")

            for row in rows:
                all_records.append({
                    "asset": asset,
                    "timestamp": datetime.fromtimestamp(int(row[0]), tz=timezone.utc),
                    "open": float(row[3]),
                    "high": float(row[2]),
                    "low": float(row[1]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "source": "coinbase",
                })
            current_start = chunk_end

    if not all_records:
        raise ValueError(f"No data returned for {asset} from Coinbase")

    df = make_canonical(pd.DataFrame(all_records))
    df = _exclude_incomplete_candle(df)
    df = _deduplicate(df)
    return df


async def fetch_all(
    provider: str = "kraken",
    assets: list[str] | None = None,
    days: int = 730,
) -> dict[str, pd.DataFrame]:
    assets = assets or list(ASSETS.keys())
    fetcher = fetch_kraken if provider == "kraken" else fetch_coinbase
    results = {}

    for asset in assets:
        logger.info("Fetching %s from %s (%d days)...", asset, provider, days)
        try:
            df = await fetcher(asset, days)
            results[asset] = df
            received = len(df)
            logger.info("  Got %d candles for %s", received, asset)

            expected_min = int(days * 0.9)
            if received < expected_min:
                logger.warning(
                    "  %s: received %d candles but expected ~%d for %d days. "
                    "Provider may have limited history.",
                    asset, received, days, days,
                )
        except Exception as e:
            logger.error("  Failed to fetch %s: %s", asset, e)

    return results


def save_all(datasets: dict[str, pd.DataFrame], output_dir: Path | None = None) -> list[Path]:
    output_dir = output_dir or DATA_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for asset, df in datasets.items():
        safe_name = asset.replace("/", "_")
        path = output_dir / f"{safe_name}.csv"
        save_data(df, path)
        saved.append(path)
        logger.info("Saved %s -> %s (%d candles)", asset, path, len(df))
    return saved


def main():
    parser = argparse.ArgumentParser(description="Fetch historical OHLCV data")
    parser.add_argument("--provider", default="kraken", choices=["kraken", "coinbase"])
    parser.add_argument("--assets", default=None, help="Comma-separated: BTC/USD,ETH/USD")
    parser.add_argument("--days", type=int, default=730, help="Days of history")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    assets = args.assets.split(",") if args.assets else None
    output_dir = Path(args.output_dir) if args.output_dir else None

    datasets = asyncio.run(fetch_all(args.provider, assets, args.days))
    if not datasets:
        logger.error("No data fetched. Check network connectivity.")
        sys.exit(1)

    saved = save_all(datasets, output_dir)
    print(f"\nFetched {len(datasets)} assets, saved to:")
    for p in saved:
        print(f"  {p}")


if __name__ == "__main__":
    main()
