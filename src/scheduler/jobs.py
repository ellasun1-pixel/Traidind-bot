from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings, AgentMode, AssetConfig
from src.market_data.pipeline import MarketDataPipeline
from src.strategy.engine import StrategyEngine, TradeSignal
from src.portfolio.manager import PaperPortfolio
from src.notifier.notification_logic import NotificationManager
from src.notifier.formatter import SignalFormatter
from src.database import get_session
from src.database.models import Signal, Asset
from src.database.repository import (
    AssetRepository, SchedulerStateRepository, AuditLogRepository,
    PriceHistoryRepository, MarketDataMetaRepository, SignalRepository,
    AppSettingRepository,
)
from src.signals.lifecycle import SignalLifecycle, SignalType
from src.health.service import get_health_service

logger = logging.getLogger(__name__)

_portfolio: PaperPortfolio | None = None
_send_message_func = None
_pipeline: MarketDataPipeline | None = None
_engine: StrategyEngine | None = None
_notification_mgr: NotificationManager | None = None
_formatter: SignalFormatter | None = None
_last_signals: dict[str, TradeSignal] = {}
_instance_id: str = str(uuid.uuid4())[:8]


def get_portfolio() -> PaperPortfolio:
    global _portfolio
    if _portfolio is None:
        _portfolio = PaperPortfolio()
    return _portfolio


def set_send_message_func(func):
    global _send_message_func
    _send_message_func = func
    if func is None:
        logger.warning("set_send_message_func called with None — all notifications will be silently dropped")
    else:
        logger.info("set_send_message_func set successfully: %s", func.__qualname__ if hasattr(func, '__qualname__') else type(func).__name__)


def get_last_signals() -> dict[str, TradeSignal]:
    return _last_signals


def get_pipeline() -> MarketDataPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = MarketDataPipeline()
    return _pipeline


def get_scheduler_status() -> list[dict]:
    try:
        with get_session() as session:
            repo = SchedulerStateRepository(session)
            states = repo.get_all()
            return [
                {
                    "job_name": s.job_name,
                    "current_status": s.current_status or "idle",
                    "run_count": s.run_count,
                    "success_count": s.success_count or 0,
                    "failure_count": s.failure_count or 0,
                    "last_run_at": s.last_run_at,
                    "last_success_at": s.last_success_at,
                    "last_error": s.last_error,
                    "last_duration_ms": s.last_duration_ms,
                }
                for s in states
            ]
    except Exception as e:
        logger.error("Failed to get scheduler status: %s", e)
        return []


def _persist_candles(session, asset_id: int, fetch_result, provider: str) -> int:
    if not fetch_result.candles:
        return 0
    price_repo = PriceHistoryRepository(session)
    return price_repo.bulk_upsert(
        asset_id=asset_id, timeframe="1d", source=provider,
        candles=fetch_result.candles,
    )


def _persist_market_meta(session, asset_id: int, fetch_result, provider: str) -> None:
    meta_repo = MarketDataMetaRepository(session)
    v = fetch_result.validation
    meta_repo.upsert(
        asset_id=asset_id, timeframe="1d", source=provider,
        candle_count=v.candle_count,
        valid_candle_count=v.valid_candle_count,
        oldest_candle=v.oldest_candle or datetime.now(timezone.utc),
        newest_candle=v.newest_candle or datetime.now(timezone.utc),
        is_sufficient=v.valid,
        validation_error="; ".join(v.errors) if v.errors else None,
    )


def _is_signal_equivalent(existing: object, new_signal: TradeSignal) -> bool:
    if existing.signal_type != new_signal.signal_type:
        return False
    if existing.entry_price is not None and new_signal.entry_price:
        price_diff = abs(float(existing.entry_price) - new_signal.entry_price)
        if new_signal.entry_price > 0:
            if price_diff / new_signal.entry_price > 0.02:
                return False
    if existing.stop_loss is not None and new_signal.stop_loss:
        sl_diff = abs(float(existing.stop_loss) - new_signal.stop_loss)
        if new_signal.stop_loss > 0:
            if sl_diff / new_signal.stop_loss > 0.02:
                return False
    existing_priority = existing.priority if hasattr(existing, "priority") else ""
    new_priority = new_signal.priority.lower() if new_signal.priority else "normal"
    if existing_priority != new_priority:
        return False
    return True


def _resolve_asset_id(session, symbol: str) -> int | None:
    repo = AssetRepository(session)
    asset = repo.get_by_symbol(symbol)
    if asset:
        return asset.id
    return None


async def _process_single_asset(asset: AssetConfig) -> dict:
    global _last_signals
    pipeline = get_pipeline()
    engine = _engine or StrategyEngine()

    result = {
        "symbol": asset.symbol,
        "status": "ok",
        "signal_type": None,
        "error": None,
        "candles_persisted": 0,
    }

    fetch_result = await pipeline.fetch_validated_candles(asset, "1d")

    with get_session() as session:
        asset_id = _resolve_asset_id(session, asset.symbol)
        if asset_id is not None:
            _persist_market_meta(session, asset_id, fetch_result, fetch_result.provider_used)
            if fetch_result.validation.valid:
                result["candles_persisted"] = _persist_candles(
                    session, asset_id, fetch_result, fetch_result.provider_used,
                )

    safety = await pipeline.get_analysis_ready_data(asset)

    if not safety.safe:
        logger.warning("Data not safe for %s: %s", asset.symbol, safety.reason)
        result["status"] = "data_unsafe"
        result["error"] = safety.reason
        return result

    portfolio = get_portfolio()
    open_positions = portfolio.get_open_positions()
    total_risk = portfolio.get_total_open_risk()

    signal = engine.analyze(
        symbol=asset.symbol,
        daily_df=safety.daily_df,
        h4_df=safety.daily_df,
        current_price=safety.current_price,
        portfolio_balance=portfolio.balance_usd,
        open_positions=open_positions,
        total_open_risk_usd=total_risk,
    )

    _last_signals[asset.symbol] = signal
    result["signal_type"] = signal.signal_type

    if signal.signal_type in ("NO_TRADE", "WAIT"):
        logger.debug("asset=%s signal=%s — skipping DB persist and notification", asset.symbol, signal.signal_type)
        return result

    with get_session() as session:
        asset_id = _resolve_asset_id(session, asset.symbol)
        if asset_id is None:
            return result

        lifecycle = SignalLifecycle(session)
        pending = lifecycle.get_pending_for_asset(asset_id)

        regime_val = signal.regime.value if hasattr(signal.regime, "value") else str(signal.regime)
        expires = datetime.now(timezone.utc) + timedelta(minutes=settings.signal_expiry_minutes)
        previous_signal_id = None

        if pending:
            existing = pending[0]
            if _is_signal_equivalent(existing, signal):
                logger.warning(
                    "DUPLICATE_SUPPRESSED asset=%s new_type=%s new_price=%.2f "
                    "existing_id=%s existing_type=%s existing_price=%s existing_created=%s "
                    "existing_expires=%s",
                    asset.symbol, signal.signal_type, signal.entry_price or 0,
                    existing.id, existing.signal_type,
                    existing.entry_price, existing.created_at, existing.expires_at,
                )
                result["status"] = "duplicate_suppressed"
                return result
            previous_signal_id = existing.id
            logger.info(
                "Materially changed signal for %s: %s -> %s, superseding %s",
                asset.symbol, existing.signal_type, signal.signal_type, existing.id,
            )

        lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=signal.signal_type,
            regime=regime_val,
            expires_at=expires,
            reason=signal.reason,
            explanation=signal.explanation or "",
            strategy_version=settings.strategy_version,
            entry_price=signal.entry_price or None,
            stop_loss=signal.stop_loss or None,
            position_size_usd=signal.position_size_usd or None,
            max_loss_usd=signal.max_loss_usd or None,
            priority=signal.priority.lower() if signal.priority else "normal",
            order_type=signal.order_type or None,
            cancel_level=signal.cancel_level or None,
            price_range_low=signal.price_range_low or None,
            price_range_high=signal.price_range_high or None,
            previous_signal_id=previous_signal_id,
            supersede_previous=True,
        )
        if previous_signal_id:
            result["status"] = "superseded_previous"

    notification_mgr = _notification_mgr or NotificationManager()
    should_send, reason = notification_mgr.should_send(signal)

    if not should_send:
        logger.warning(
            "NOTIFICATION_BLOCKED asset=%s type=%s reason=%s",
            asset.symbol, signal.signal_type, reason,
        )
    elif _send_message_func is None:
        logger.error(
            "NOTIFICATION_DROPPED asset=%s type=%s — _send_message_func is None, "
            "Telegram bot not initialized. Signal will NOT be delivered.",
            asset.symbol, signal.signal_type,
        )
    else:
        formatter = _formatter or SignalFormatter()
        message = formatter.format_signal(signal)
        try:
            await _send_message_func(message)
            logger.info("NOTIFICATION_SENT asset=%s type=%s reason=%s", asset.symbol, signal.signal_type, reason)
        except Exception as e:
            logger.error(
                "NOTIFICATION_FAILED asset=%s type=%s error=%s — Telegram API call failed, signal lost",
                asset.symbol, signal.signal_type, e,
            )

    return result


async def market_check_job():
    global _engine

    if settings.agent_mode == AgentMode.PAUSED:
        logger.info("Agent is PAUSED, skipping market check")
        return

    portfolio = get_portfolio()
    if not portfolio.is_challenge_active:
        logger.info("Challenge is %s — skipping signal generation", portfolio.challenge_status)
        return

    if _engine is None:
        _engine = StrategyEngine()

    job_name = "market_check"
    start_time = time.monotonic()

    with get_session() as session:
        sched_repo = SchedulerStateRepository(session)
        acquired = sched_repo.try_acquire_lock(job_name, _instance_id, lock_duration_seconds=600)
        if not acquired:
            logger.info("market_check already locked, skipping")
            return

    asset_results = []
    errors = []

    for asset in settings.assets:
        if not asset.active:
            continue
        try:
            result = await _process_single_asset(asset)
            asset_results.append(result)
        except Exception as e:
            logger.error("Error processing %s: %s", asset.symbol, e, exc_info=True)
            asset_results.append({
                "symbol": asset.symbol, "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })
            errors.append(f"{asset.symbol}: {e}")

    duration_ms = int((time.monotonic() - start_time) * 1000)

    with get_session() as session:
        sched_repo = SchedulerStateRepository(session)
        if errors:
            sched_repo.mark_failure(
                job_name, "; ".join(errors), duration_ms=duration_ms,
            )
        else:
            sched_repo.mark_success(job_name, duration_ms=duration_ms)

    logger.info(
        "market_check completed in %dms: %d assets, %d errors",
        duration_ms, len(asset_results), len(errors),
    )


async def expire_signals_job():
    job_name = "expire_signals"
    start_time = time.monotonic()

    try:
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            acquired = sched_repo.try_acquire_lock(job_name, _instance_id, lock_duration_seconds=60)
            if not acquired:
                return

        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            expired = lifecycle.expire_old_signals()
            count = len(expired)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_success(job_name, duration_ms=duration_ms)

        if count > 0:
            logger.info("Expired %d signals", count)

    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_failure(job_name, str(e), duration_ms=duration_ms)
        logger.error("expire_signals failed: %s", e, exc_info=True)


async def _build_report(report_type: str) -> str:
    portfolio = get_portfolio()
    pipeline = get_pipeline()
    prices = {}
    for asset in settings.assets:
        try:
            kraken_q, coinbase_q = await pipeline.get_prices(asset)
            quote = kraken_q or coinbase_q
            if quote:
                prices[asset.symbol] = quote.price
        except Exception:
            pass

    summary = portfolio.get_portfolio_summary(prices)
    formatter = _formatter or SignalFormatter()

    health_service = get_health_service()
    system = health_service.check_all()
    health_status = system.status.value

    pending_signals = []
    try:
        with get_session() as session:
            from src.signals.lifecycle import SignalLifecycle
            lifecycle = SignalLifecycle(session)
            for sig in session.query(Signal).filter(Signal.status == "pending").all():
                asset_obj = session.query(Asset).filter(Asset.id == sig.asset_id).first()
                pending_signals.append({
                    "asset": asset_obj.symbol if asset_obj else "Unknown",
                    "type": sig.signal_type,
                    "expires_at": sig.expires_at,
                })
    except Exception:
        pass

    scheduler_info = {}
    try:
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            for s in sched_repo.get_all():
                if s.job_name == "market_check":
                    if s.last_success_at:
                        ts = s.last_success_at
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        scheduler_info["last_market_check"] = ts.strftime("%H:%M UTC")
                    scheduler_info["next_market_check"] = f"~{settings.check_interval_minutes} min"
    except Exception:
        pass

    return formatter.format_report(
        report_type=report_type,
        summary=summary,
        health_status=health_status,
        last_signals=_last_signals or None,
        pending_signals=pending_signals or None,
        scheduler_info=scheduler_info or None,
    )


async def morning_report_job():
    job_name = "morning_report"
    start_time = time.monotonic()

    try:
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            acquired = sched_repo.try_acquire_lock(job_name, _instance_id, lock_duration_seconds=120)
            if not acquired:
                return

        tz = pytz.timezone(settings.timezone)
        today_local = datetime.now(tz).strftime("%Y-%m-%d")

        with get_session() as session:
            setting_repo = AppSettingRepository(session)
            last_date = setting_repo.get("last_morning_report_date")
            if last_date == today_local:
                logger.info("Morning report already sent today")
                sched_repo2 = SchedulerStateRepository(session)
                sched_repo2.mark_success(job_name)
                return

        if _send_message_func is None:
            logger.error("morning_report SKIPPED — _send_message_func is None, Telegram bot not initialized")
            return

        message = await _build_report("morning")
        await _send_message_func(message)

        with get_session() as session:
            setting_repo = AppSettingRepository(session)
            setting_repo.set("last_morning_report_date", today_local)

        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_success(job_name, duration_ms=duration_ms)

    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_failure(job_name, str(e), duration_ms=duration_ms)
        logger.error("morning_report failed: %s", e, exc_info=True)


async def evening_report_job():
    job_name = "evening_report"
    start_time = time.monotonic()

    try:
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            acquired = sched_repo.try_acquire_lock(job_name, _instance_id, lock_duration_seconds=120)
            if not acquired:
                return

        tz = pytz.timezone(settings.timezone)
        today_local = datetime.now(tz).strftime("%Y-%m-%d")

        with get_session() as session:
            setting_repo = AppSettingRepository(session)
            last_date = setting_repo.get("last_evening_report_date")
            if last_date == today_local:
                logger.info("Evening report already sent today")
                sched_repo2 = SchedulerStateRepository(session)
                sched_repo2.mark_success(job_name)
                return

        if _send_message_func is None:
            logger.error("evening_report SKIPPED — _send_message_func is None, Telegram bot not initialized")
            return

        message = await _build_report("evening")
        await _send_message_func(message)

        with get_session() as session:
            setting_repo = AppSettingRepository(session)
            setting_repo.set("last_evening_report_date", today_local)

        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_success(job_name, duration_ms=duration_ms)

    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_failure(job_name, str(e), duration_ms=duration_ms)
        logger.error("evening_report failed: %s", e, exc_info=True)


async def health_heartbeat_job():
    job_name = "health_heartbeat"
    start_time = time.monotonic()
    try:
        pipeline = get_pipeline()
        health_data = {}
        for asset in settings.assets:
            if not asset.active:
                continue
            health = pipeline.get_health(asset.symbol)
            health_data[asset.symbol] = {
                "provider": health.current_provider,
                "freshness_h": health.candle_freshness_hours,
                "status": health.validation_status,
                "error": health.latest_error,
            }

        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_success(job_name, duration_ms=duration_ms)
            audit_repo = AuditLogRepository(session)
            audit_repo.log("HEALTH_HEARTBEAT", "scheduler", health_data)

        logger.debug("Health heartbeat: %s", health_data)

    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_failure(job_name, str(e), duration_ms=duration_ms)
        logger.error("health_heartbeat failed: %s", e, exc_info=True)


async def health_check_job():
    job_name = "health_check"
    start_time = time.monotonic()
    try:
        service = get_health_service()
        system = service.run_check_and_record()
        await service.notify_if_needed(system)

        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_success(job_name, duration_ms=duration_ms)

        logger.debug("Health check: %s", system.status.value)
    except Exception as e:
        duration_ms = int((time.monotonic() - start_time) * 1000)
        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            sched_repo.mark_failure(job_name, str(e), duration_ms=duration_ms)
        logger.error("health_check failed: %s", e, exc_info=True)


async def startup_sweep():
    logger.info("Running startup sweep...")
    try:
        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            expired = lifecycle.expire_old_signals()
            if expired:
                logger.info("Startup sweep expired %d signals", len(expired))

        with get_session() as session:
            sched_repo = SchedulerStateRepository(session)
            for state in sched_repo.get_all():
                if state.current_status == "running":
                    state.current_status = "idle"
                    state.lock_owner = None
                    state.lock_expires_at = None
            session.flush()
            logger.info("Cleared stale locks from previous run")

    except Exception as e:
        logger.error("Startup sweep failed: %s", e, exc_info=True)


def setup_scheduler() -> AsyncIOScheduler:
    tz = pytz.timezone(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        market_check_job,
        IntervalTrigger(minutes=settings.check_interval_minutes),
        id="market_check",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        expire_signals_job,
        IntervalTrigger(minutes=5),
        id="expire_signals",
        replace_existing=True,
        misfire_grace_time=30,
    )

    scheduler.add_job(
        morning_report_job,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="morning_report",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        evening_report_job,
        CronTrigger(hour=22, minute=30, timezone=tz),
        id="evening_report",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        health_heartbeat_job,
        IntervalTrigger(minutes=10),
        id="health_heartbeat",
        replace_existing=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        health_check_job,
        IntervalTrigger(minutes=1),
        id="health_check",
        replace_existing=True,
        misfire_grace_time=30,
    )

    return scheduler
