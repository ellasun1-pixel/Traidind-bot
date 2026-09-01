"""Market event calendar data providers.

Three sources:
1. JBlanked API — macro-economic events (CPI, NFP, FOMC, speeches)
2. CoinMarketCal — crypto-specific events (forks, listings, unlocks)
3. Hardcoded FOMC dates — reliable fallback for Fed meetings
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

JBLANKED_API_KEY = os.getenv("JBLANKED_API_KEY", "")
COINMARKETCAL_API_KEY = os.getenv("COINMARKETCAL_API_KEY", "")

FOMC_DATES_2026 = [
    datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc),
    datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 12, 16, 19, 0, tzinfo=timezone.utc),
]

FOMC_DATES_2027 = [
    datetime(2027, 1, 27, 19, 0, tzinfo=timezone.utc),
    datetime(2027, 3, 17, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 5, 5, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 6, 16, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 7, 28, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 9, 22, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 11, 3, 18, 0, tzinfo=timezone.utc),
    datetime(2027, 12, 15, 19, 0, tzinfo=timezone.utc),
]

HIGH_IMPACT_KEYWORDS = [
    "interest rate", "fed funds", "fomc", "federal reserve",
    "cpi", "consumer price", "inflation",
    "non-farm", "nonfarm", "payroll", "employment",
    "gdp", "gross domestic",
    "pce", "personal consumption",
    "retail sales",
    "powell", "yellen", "sec", "cftc", "gensler",
]

CRYPTO_SYMBOLS_MAP: dict[str, list[str]] = {}


@dataclass
class MarketEvent:
    title: str
    event_time: datetime
    category: str
    source: str
    impact: str = "medium"
    currency: str = "USD"
    asset_symbol: str | None = None
    description: str = ""
    forecast: str = ""
    previous: str = ""
    source_id: str = ""

    @property
    def unique_key(self) -> str:
        date_str = self.event_time.strftime("%Y%m%d")
        return f"{self.source}:{date_str}:{self.title[:60]}"


def _build_crypto_map(tracked_symbols: list[str]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for sym in tracked_symbols:
        coin = sym.split("/")[0].upper()
        mapping[coin.lower()] = [sym]
        if coin == "BTC":
            mapping["bitcoin"] = [sym]
        elif coin == "ETH":
            mapping["ethereum"] = [sym]
        elif coin == "XRP":
            mapping["ripple"] = [sym]
        elif coin == "DOGE":
            mapping["dogecoin"] = [sym]
        elif coin == "SOL":
            mapping["solana"] = [sym]
        elif coin == "ADA":
            mapping["cardano"] = [sym]
        elif coin == "DOT":
            mapping["polkadot"] = [sym]
        elif coin == "LINK":
            mapping["chainlink"] = [sym]
        elif coin == "LTC":
            mapping["litecoin"] = [sym]
        elif coin == "AVAX":
            mapping["avalanche"] = [sym]
        elif coin == "ZEC":
            mapping["zcash"] = [sym]
        elif coin == "ATOM":
            mapping["cosmos"] = [sym]
        elif coin == "UNI":
            mapping["uniswap"] = [sym]
        elif coin == "AAVE":
            mapping["aave"] = [sym]
        elif coin == "NEAR":
            mapping["near protocol"] = [sym]
        elif coin == "SUI":
            mapping["sui"] = [sym]
    return mapping


def init_crypto_map(tracked_symbols: list[str]) -> None:
    global CRYPTO_SYMBOLS_MAP
    CRYPTO_SYMBOLS_MAP = _build_crypto_map(tracked_symbols)


def _is_high_impact(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in HIGH_IMPACT_KEYWORDS)


async def fetch_jblanked_events(days_ahead: int = 7) -> list[MarketEvent]:
    if not JBLANKED_API_KEY:
        logger.warning("JBLANKED_API_KEY not set, skipping economic calendar fetch")
        return []

    events: list[MarketEvent] = []
    url = "https://www.jblanked.com/news/api/calendar/week/"
    headers = {"Authorization": f"Api-Key {JBLANKED_API_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=30, verify=os.environ.get("SSL_CERT_FILE", True)) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        if not isinstance(data, list):
            data = data.get("events", data.get("results", []))

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days_ahead)

        for item in data:
            title = item.get("title") or item.get("event") or ""
            currency = item.get("currency", "USD")

            if currency != "USD":
                continue

            date_str = item.get("date") or item.get("datetime") or ""
            try:
                if "T" in date_str:
                    event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                else:
                    # strptime returns naive datetime; tzinfo is None by definition
                    event_time = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)

            if event_time < now or event_time > cutoff:
                continue

            raw_impact = str(item.get("impact", "")).lower()
            if raw_impact in ("high", "3"):
                impact = "high"
            elif raw_impact in ("medium", "2"):
                impact = "medium"
            else:
                impact = "low"

            if impact == "low" and not _is_high_impact(title):
                continue

            events.append(MarketEvent(
                title=title,
                event_time=event_time,
                category="macro",
                source="jblanked",
                impact=impact,
                currency=currency,
                description=item.get("explanation", ""),
                forecast=str(item.get("forecast", "")),
                previous=str(item.get("previous", "")),
                source_id=str(item.get("id", "")),
            ))

        logger.info("JBlanked: fetched %d USD high/medium-impact events", len(events))

    except httpx.HTTPStatusError as e:
        logger.error("JBlanked API error: HTTP %s", e.response.status_code)
    except Exception as e:
        logger.error("JBlanked fetch failed: %s", e)

    return events


async def fetch_coinmarketcal_events(days_ahead: int = 14) -> list[MarketEvent]:
    if not COINMARKETCAL_API_KEY:
        logger.warning("COINMARKETCAL_API_KEY not set, skipping crypto events fetch")
        return []

    events: list[MarketEvent] = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)

    url = "https://developers.coinmarketcal.com/v1/events"
    headers = {
        "x-api-key": COINMARKETCAL_API_KEY,
        "Accept": "application/json",
    }
    params = {
        "dateRangeStart": now.strftime("%Y-%m-%d"),
        "dateRangeEnd": cutoff.strftime("%Y-%m-%d"),
        "page": 1,
        "max": 75,
        "sortBy": "date_event",
    }

    try:
        async with httpx.AsyncClient(timeout=30, verify=os.environ.get("SSL_CERT_FILE", True)) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("body", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []

        for item in items:
            title = item.get("title", {}).get("en", "") if isinstance(item.get("title"), dict) else str(item.get("title", ""))
            date_str = item.get("date_event", "")
            try:
                event_time = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                try:
                    # strptime returns naive datetime; tzinfo is None by definition
                    event_time = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue

            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)

            coins = item.get("coins", [])
            coin_names = []
            matched_symbol = None
            for coin in coins:
                name = (coin.get("name", "") or "").lower()
                symbol = (coin.get("symbol", "") or "").lower()
                coin_names.append(f"{coin.get('symbol', '')} ({coin.get('name', '')})")
                if symbol in CRYPTO_SYMBOLS_MAP:
                    matched_symbol = CRYPTO_SYMBOLS_MAP[symbol][0]
                elif name in CRYPTO_SYMBOLS_MAP:
                    matched_symbol = CRYPTO_SYMBOLS_MAP[name][0]

            if not matched_symbol and not coins:
                continue
            if coins and not matched_symbol:
                continue

            categories = item.get("categories", [])
            cat_names = [c.get("name", "") for c in categories] if isinstance(categories, list) else []

            high_impact_cats = {"Hard Fork", "Airdrop", "Mainnet Launch", "Exchange Listing",
                                "Token Burn", "Staking", "Partnership", "Regulation"}
            impact = "high" if any(c in high_impact_cats for c in cat_names) else "medium"

            events.append(MarketEvent(
                title=title,
                event_time=event_time,
                category="crypto",
                source="coinmarketcal",
                impact=impact,
                asset_symbol=matched_symbol,
                description=", ".join(cat_names),
                source_id=str(item.get("id", "")),
            ))

        logger.info("CoinMarketCal: fetched %d events for tracked assets", len(events))

    except httpx.HTTPStatusError as e:
        logger.error("CoinMarketCal API error: HTTP %s", e.response.status_code)
    except Exception as e:
        logger.error("CoinMarketCal fetch failed: %s", e)

    return events


def fetch_fomc_events(days_ahead: int = 30) -> list[MarketEvent]:
    return get_fomc_events(days_ahead)


def get_fomc_events(days_ahead: int = 30) -> list[MarketEvent]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    events = []

    for dt in FOMC_DATES_2026 + FOMC_DATES_2027:
        if now <= dt <= cutoff:
            events.append(MarketEvent(
                title="FOMC Interest Rate Decision",
                event_time=dt,
                category="macro",
                source="fed_hardcoded",
                impact="high",
                currency="USD",
                description="Federal Reserve FOMC meeting — interest rate decision announcement",
                source_id=f"fomc_{dt.strftime('%Y%m%d')}",
            ))

    return events


async def fetch_all_events(days_ahead: int = 14) -> list[MarketEvent]:
    all_events: list[MarketEvent] = []

    fomc = get_fomc_events(days_ahead)
    all_events.extend(fomc)

    jblanked = await fetch_jblanked_events(days_ahead)
    all_events.extend(jblanked)

    crypto = await fetch_coinmarketcal_events(days_ahead)
    all_events.extend(crypto)

    seen_keys: set[str] = set()
    unique: list[MarketEvent] = []
    for ev in all_events:
        key = ev.unique_key
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(ev)

    unique.sort(key=lambda e: e.event_time)
    logger.info("Total calendar events: %d (fomc=%d, macro=%d, crypto=%d)",
                len(unique), len(fomc), len(jblanked), len(crypto))
    return unique
