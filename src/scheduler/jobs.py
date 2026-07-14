from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings, AgentMode
from src.market_data.fetcher import MarketDataFetcher
from src.strategy.engine import StrategyEngine, TradeSignal
from src.portfolio.manager import PaperPortfolio
from src.notifier.notification_logic import NotificationManager
from src.notifier.formatter import SignalFormatter

logger = logging.getLogger(__name__)

_portfolio: PaperPortfolio | None = None
_send_message_func = None
_fetcher: MarketDataFetcher | None = None
_engine: StrategyEngine | None = None
_notification_mgr: NotificationManager | None = None
_formatter: SignalFormatter | None = None
_last_signals: dict[str, TradeSignal] = {}


def get_portfolio() -> PaperPortfolio:
    global _portfolio
    if _portfolio is None:
        _portfolio = PaperPortfolio()
    return _portfolio


def set_send_message_func(func):
    global _send_message_func
    _send_message_func = func


def get_last_signals() -> dict[str, TradeSignal]:
    return _last_signals


async def market_check_job():
    global _fetcher, _engine, _notification_mgr, _formatter, _last_signals

    if settings.agent_mode == AgentMode.PAUSED:
        logger.info("Agent is PAUSED, skipping market check")
        return

    if _fetcher is None:
        _fetcher = MarketDataFetcher()
    if _engine is None:
        _engine = StrategyEngine()
    if _notification_mgr is None:
        _notification_mgr = NotificationManager()
    if _formatter is None:
        _formatter = SignalFormatter()

    portfolio = get_portfolio()

    for asset in settings.assets:
        if not asset.active:
            continue
        try:
            daily_df, source = await _fetcher.fetch_ohlc(asset, "1d")
            h4_df, _ = await _fetcher.fetch_ohlc(asset, "4h")

            if daily_df.empty:
                logger.warning("No data for %s, skipping", asset.symbol)
                continue

            diverges, div_pct = await _fetcher.check_source_divergence(asset)
            if diverges:
                logger.warning(
                    "Source divergence for %s: %.2f%% — suppressing signal",
                    asset.symbol, div_pct * 100,
                )
                continue

            current_price = await _fetcher.get_latest_price(asset)
            if current_price is None:
                continue

            open_positions = portfolio.get_open_positions()
            total_risk = portfolio.get_total_open_risk()

            signal = _engine.analyze(
                symbol=asset.symbol,
                daily_df=daily_df,
                h4_df=h4_df,
                current_price=current_price,
                portfolio_balance=portfolio.balance_usd,
                open_positions=open_positions,
                total_open_risk_usd=total_risk,
            )

            _last_signals[asset.symbol] = signal

            should_send, reason = _notification_mgr.should_send(signal)
            if should_send and _send_message_func:
                message = _formatter.format_signal(signal)
                await _send_message_func(message)
                logger.info("Signal sent for %s: %s (%s)", asset.symbol, signal.signal_type, reason)
            else:
                logger.debug("Signal suppressed for %s: %s (%s)", asset.symbol, signal.signal_type, reason)

        except Exception as e:
            logger.error("Error processing %s: %s", asset.symbol, e, exc_info=True)


async def morning_report_job():
    if _send_message_func is None:
        return
    portfolio = get_portfolio()
    prices = {}
    if _fetcher:
        for asset in settings.assets:
            price = await _fetcher.get_latest_price(asset)
            if price:
                prices[asset.symbol] = price

    summary = portfolio.get_portfolio_summary(prices)
    formatter = _formatter or SignalFormatter()
    message = "☀️ *Morning Report*\n\n" + formatter.format_portfolio_summary(summary)

    overnight = []
    for symbol, sig in _last_signals.items():
        if sig.signal_type != "NO_TRADE":
            overnight.append(f"• {symbol}: {sig.signal_type} ({sig.reason})")
    if overnight:
        message += "\n\n*Overnight signals (check if still valid):*\n" + "\n".join(overnight)
    else:
        message += "\n\n_No overnight signals._"

    await _send_message_func(message)


async def evening_report_job():
    if _send_message_func is None:
        return
    portfolio = get_portfolio()
    prices = {}
    if _fetcher:
        for asset in settings.assets:
            price = await _fetcher.get_latest_price(asset)
            if price:
                prices[asset.symbol] = price

    summary = portfolio.get_portfolio_summary(prices)
    formatter = _formatter or SignalFormatter()
    message = "🌙 *Evening Report*\n\n" + formatter.format_portfolio_summary(summary)
    message += "\n\n_Entering night mode. Only emergency alerts until 08:00._"
    await _send_message_func(message)


def setup_scheduler() -> AsyncIOScheduler:
    tz = pytz.timezone(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        market_check_job,
        IntervalTrigger(minutes=settings.check_interval_minutes),
        id="market_check",
        replace_existing=True,
    )

    scheduler.add_job(
        morning_report_job,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="morning_report",
        replace_existing=True,
    )

    scheduler.add_job(
        evening_report_job,
        CronTrigger(hour=22, minute=30, timezone=tz),
        id="evening_report",
        replace_existing=True,
    )

    return scheduler
