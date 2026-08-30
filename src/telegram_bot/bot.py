from __future__ import annotations

import logging
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from src.config import settings, AgentMode
from src.scheduler.jobs import get_portfolio, get_last_signals, clear_last_signals, get_scheduler_status, get_pipeline, record_portfolio_snapshot, get_live_prices, get_active_mode, set_active_mode
from src.calendar.manager import get_calendar_manager, format_calendar_view
from src.notifier.formatter import SignalFormatter
from src.database import get_session, AuditLog
from src.database.models import Signal, Asset
from src.signals.lifecycle import SignalLifecycle
from src.auth.owner import owner_only, validate_auth_config
from src.auth.permissions import Permission, get_user_permissions
from src.database.session import check_db_health
from src.health.service import get_health_service
from src.strategy.indicators import compute_indicators, indicator_warmup_status, WARMUP
from src.strategy.regime import classify_regime, MarketRegime, regime_nan_fields
from src.strategy.engine import StrategyEngine
import pandas as pd
from src.signals.lifecycle import InvalidTransitionError

logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


formatter = SignalFormatter()


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "\U0001f44b *Paper Challenge Agent*\n\n"
        "I monitor crypto markets and send you trading signals for the Kraken Funded Challenge.\n"
        "I never place real trades — you execute manually and confirm here.\n\n"
        "Use /help to see available commands.",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "\U0001f4cb *Available Commands*\n\n"
        "/status — Current regime, balance, active signals\n"
        "/portfolio — Full portfolio view\n"
        "/signal — Latest signal for each asset\n"
        "/history — Recent trade history\n"
        "/confirm — Confirm a pending signal\n"
        "/reject — Reject a pending signal\n"
        "/pause — Pause signal generation\n"
        "/resume — Resume signal generation\n"
        "/settings — View/toggle settings\n"
        "/auth — Authentication diagnostics\n"
        "/scheduler — Job execution status\n"
        "/health — Operational health dashboard\n"
        "/debug — Regime diagnostics (all or /debug BTC)\n"
        "/manual\\_buy TICKER AMOUNT \\[@price\\] — Record a buy (optional real price)\n"
        "/manual\\_sell TICKER — Record a sell at current market price\n"
        "/switch\\_mode live|paper — Switch between LIVE and PAPER portfolios\n"
        "/sync\\_portfolio TICKER QTY CASH — Sync with actual exchange state\n"
        "/calendar — Upcoming market events (48h)\n"
        "/new\\_challenge — Reset and start a new paper challenge\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        prices = await get_live_prices()
        equity = portfolio.get_total_equity(prices)

        portfolio._update_challenge_status(prices)

        active_mode = get_active_mode()
        mode_label = "LIVE" if active_mode == "LIVE_FUNDED" else "PAPER"

        status_lines = [
            "\U0001f4ca *Status*",
            "",
            f"Portfolio: {_esc(mode_label)}",
            f"Agent: {_esc(settings.agent_mode.value)}",
            f"Equity: ${equity:.2f}",
            f"Cash: ${portfolio.balance_usd:.2f}",
            f"Challenge: {portfolio.challenge_status.upper()}",
            f"Open positions: {len([p for p in portfolio.positions if p.status == 'open'])}",
            "",
        ]

        db_signals: dict[str, tuple[str, str]] = {}
        try:
            with get_session() as session:
                from sqlalchemy import func as sqla_func
                subq = (
                    session.query(
                        Signal.asset_id,
                        sqla_func.max(Signal.created_at).label("latest"),
                    )
                    .filter(Signal.status == "pending")
                    .group_by(Signal.asset_id)
                    .subquery()
                )
                latest = (
                    session.query(Signal)
                    .join(Asset)
                    .join(subq, (Signal.asset_id == subq.c.asset_id) & (Signal.created_at == subq.c.latest))
                    .all()
                )
                for sig in latest:
                    sym = sig.asset.symbol if sig.asset else "?"
                    db_signals[sym] = (sig.regime or "UNKNOWN", sig.signal_type)
        except Exception:
            pass

        in_memory = get_last_signals()
        last_signals: dict = {}
        for symbol, val in in_memory.items():
            last_signals[symbol] = (
                val.regime.value if val.regime else "UNKNOWN",
                val.signal_type,
            )
        for sym, val in db_signals.items():
            if sym not in last_signals:
                last_signals[sym] = val
        if last_signals:
            status_lines.append("*Latest regimes:*")
            for symbol, val in last_signals.items():
                if isinstance(val, tuple):
                    regime_val, sig_type = val
                else:
                    regime_val = val.regime.value if val.regime else "UNKNOWN"
                    sig_type = val.signal_type
                status_lines.append(f"  {symbol}: {_esc(regime_val)} - {_esc(sig_type)}")
        else:
            status_lines.append("No signals generated yet.")

        await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_status crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Status error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        prices = await get_live_prices()
        summary = portfolio.get_portfolio_summary(prices)
        text = formatter.format_portfolio_summary(summary)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_portfolio crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Portfolio error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        last_signals = get_last_signals()
        if not last_signals:
            await update.message.reply_text("_No signals generated yet._", parse_mode="Markdown")
            return

        for symbol, sig in last_signals.items():
            text = formatter.format_signal(sig)
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_signal crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Signal error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        if not portfolio.closed_trades:
            await update.message.reply_text("_No completed trades yet._", parse_mode="Markdown")
            return

        lines = ["\U0001f4dc *Trade History*", ""]
        for trade in portfolio.closed_trades[-10:]:
            pnl = trade.realized_pnl or 0
            emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
            lines.append(
                f"{emoji} {trade.symbol}: {trade.quantity:.6f} @ ${trade.entry_price:.2f} "
                f"→ ${trade.exit_price:.2f} | P&L: ${pnl:.2f}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_history crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"History error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        prices = await get_live_prices()

        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            lifecycle.expire_old_signals()

            pending_db = (
                session.query(Signal)
                .join(Asset)
                .filter(Signal.status == "pending")
                .all()
            )

            from datetime import timezone as tz
            now = datetime.now(tz.utc)
            expired = []
            actionable = []
            for sig in pending_db:
                exp = sig.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=tz.utc)
                if exp <= now:
                    expired.append(sig)
                else:
                    actionable.append(sig)

            if not actionable and not expired:
                await update.message.reply_text("No pending signals to confirm.", parse_mode=None)
                return

            if not actionable and expired:
                await update.message.reply_text(
                    f"No pending signals — {len(expired)} signal(s) expired before confirmation.",
                    parse_mode=None,
                )
                return

            results = []
            challenge_ended = False
            for sig in actionable:
                if sig.status != "pending":
                    logger.warning("RACE_GUARD: signal %s already %s, skipping", sig.id, sig.status)
                    continue
                symbol = sig.asset.symbol
                entry_price = float(sig.entry_price) if sig.entry_price else 0.0
                stop_loss = float(sig.stop_loss) if sig.stop_loss else 0.0
                position_size = float(sig.position_size_usd) if sig.position_size_usd else 0.0
                max_loss = float(sig.max_loss_usd) if sig.max_loss_usd else 0.0

                try:
                    lifecycle.confirm(sig)
                except InvalidTransitionError as e:
                    logger.warning("RACE_GUARD: signal %s already transitioned: %s", sig.id, e)
                    results.append(f"⚠️ {symbol}: Signal already processed (duplicate /confirm blocked)")
                    continue

                if sig.signal_type == "BUY":
                    ok, msg = portfolio.confirm_buy(
                        symbol=symbol,
                        entry_price=entry_price,
                        position_value_usd=position_size,
                        stop_loss=stop_loss,
                        risk_dollars=max_loss,
                        signal_id=sig.id,
                        prices=prices,
                    )
                    results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")
                elif sig.signal_type == "PARTIAL_REBALANCE_SELL":
                    ok, msg = portfolio.confirm_partial_sell(
                        symbol=symbol,
                        exit_price=entry_price,
                        sell_pct=0.30,
                        signal_id=sig.id,
                        prices=prices,
                    )
                    results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")
                elif sig.signal_type == "REDUCE":
                    snap = sig.market_snapshot or {}
                    sell_pct = snap.get("sell_pct", 0.3)
                    tp_level = snap.get("tp_level", 0)
                    if tp_level == 0:
                        reason = sig.reason or ""
                        if "level 1" in reason:
                            tp_level = 1
                        elif "level 2" in reason:
                            tp_level = 2
                    ok, msg = portfolio.confirm_partial_sell(
                        symbol=symbol,
                        exit_price=entry_price,
                        sell_pct=sell_pct,
                        signal_id=sig.id,
                        prices=prices,
                        tp_level=tp_level,
                    )
                    results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")
                elif sig.signal_type in ("SELL", "TAKE_PROFIT", "MOVE_TO_USD"):
                    ok, msg = portfolio.confirm_sell(
                        symbol=symbol,
                        exit_price=entry_price,
                        signal_id=sig.id,
                        prices=prices,
                    )
                    results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")
                else:
                    continue

                if not portfolio.is_challenge_active:
                    challenge_ended = True

            session.add(AuditLog(action="CONFIRM", actor="owner", detail={"results": results}))

        await update.message.reply_text(
            "\U0001f4cb *Confirmation Results*\n\n" + "\n".join(results),
            parse_mode="Markdown",
        )

        record_portfolio_snapshot("trade_confirm", prices=prices)

        if challenge_ended:
            ended_msg = portfolio.get_challenge_ended_message(prices=prices)
            await update.message.reply_text(ended_msg, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_confirm crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Confirm error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            pending_db = (
                session.query(Signal)
                .join(Asset)
                .filter(Signal.status == "pending")
                .all()
            )

            from datetime import timezone as tz
            now = datetime.now(tz.utc)
            actionable = []
            expired_count = 0
            for sig in pending_db:
                exp = sig.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=tz.utc)
                if exp <= now:
                    expired_count += 1
                else:
                    actionable.append(sig)

            lifecycle.expire_old_signals()

            if not actionable and expired_count == 0:
                await update.message.reply_text("No pending signals to reject.", parse_mode=None)
                return

            if not actionable and expired_count > 0:
                await update.message.reply_text(
                    f"No pending signals — {expired_count} signal(s) already expired.",
                    parse_mode=None,
                )
                return

            rejected_symbols = []
            for sig in actionable:
                lifecycle.reject(sig)
                rejected_symbols.append(sig.asset.symbol)

            session.add(AuditLog(action="REJECT", actor="owner",
                                 detail={"rejected": rejected_symbols}))

        await update.message.reply_text(
            f"Rejected signals for: {', '.join(rejected_symbols)}",
            parse_mode=None,
        )
    except Exception as e:
        logger.error("cmd_reject crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Reject error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from src.database.repository import AppSettingRepository
        previous_mode = settings.agent_mode.value
        settings.agent_mode = AgentMode.PAUSED
        try:
            with get_session() as session:
                repo = AppSettingRepository(session)
                repo.set("agent_mode", AgentMode.PAUSED.value)
                repo.set("pre_pause_mode", previous_mode)
        except Exception as e:
            logger.error("Failed to persist pause state: %s", e)
        await update.message.reply_text(
            "⏸️ Agent paused. Market observation continues but no signals will be sent.\n"
            "Use /resume to continue.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("cmd_pause crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Pause error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from src.database.repository import AppSettingRepository
        restored_mode = AgentMode.PAPER_CHALLENGE
        try:
            with get_session() as session:
                repo = AppSettingRepository(session)
                saved = repo.get("pre_pause_mode")
                if saved:
                    try:
                        restored_mode = AgentMode(saved)
                    except ValueError:
                        pass
                repo.set("agent_mode", restored_mode.value)
        except Exception as e:
            logger.error("Failed to persist resume state: %s", e)
        settings.agent_mode = restored_mode
        await update.message.reply_text(
            f"▶️ Agent resumed in {restored_mode.value} mode.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("cmd_resume crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Resume error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if args and len(args) >= 2:
            key = args[0].lower()
            value = args[1].lower()
            if key == "beginner":
                settings.beginner_explanations = value in ("true", "1", "on", "yes")
                try:
                    from src.database.repository import AppSettingRepository
                    with get_session() as session:
                        repo = AppSettingRepository(session)
                        repo.set("beginner_explanations", str(settings.beginner_explanations))
                except Exception as e:
                    logger.error("Failed to persist beginner_explanations: %s", e)
                await update.message.reply_text(
                    f"✅ BEGINNER_EXPLANATIONS = {settings.beginner_explanations}",
                    parse_mode="Markdown",
                )
                return

        text = (
            "⚙️ *Settings*\n\n"
            f"Mode: {_esc(settings.agent_mode.value)}\n"
            f"Beginner explanations: {settings.beginner_explanations}\n"
            f"Timezone: {settings.timezone}\n"
            f"Active hours: {settings.active_hours_start}:00–{settings.active_hours_end}:00\n"
            f"Check interval: {settings.check_interval_minutes} min\n"
            f"Assets: {', '.join(a.symbol for a in settings.assets)}\n"
            "\nToggle: /settings beginner true|false"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_settings crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Settings error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        chat = update.effective_chat
        user_perms = get_user_permissions(user.id)
        db_health = check_db_health()
        env = settings.app_env

        perm_lines = "\n".join(f"  {p.value}" for p in Permission if p in user_perms)

        text = (
            "\U0001f510 *Authentication Status*\n\n"
            f"Owner: Authorized\n"
            f"Telegram User ID: `{user.id}`\n"
            f"Chat ID: `{chat.id}`\n"
            f"Environment: {env.capitalize()}\n\n"
            f"*Permissions:*\n{perm_lines}\n\n"
            f"Database: {'Connected' if db_health['status'] == 'ok' else 'Error'}\n"
            f"Bot: Running\n"
            f"Strategy Version: {settings.strategy_version}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_auth crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Auth error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_scheduler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        statuses = get_scheduler_status()
        if not statuses:
            await update.message.reply_text("_No scheduler data yet._", parse_mode="Markdown")
            return

        lines = ["\U0001f553 *Scheduler Status*", ""]
        for s in statuses:
            status_emoji = "\U0001f7e2" if s["current_status"] == "idle" else "\U0001f7e1"
            if s["last_error"]:
                status_emoji = "\U0001f534"
            lines.append(f"{status_emoji} *{_esc(s['job_name'])}*")
            lines.append(f"  Status: {_esc(s['current_status'])}")
            lines.append(f"  Runs: {s['run_count']} (OK: {s['success_count']}, Fail: {s['failure_count']})")
            if s["last_duration_ms"] is not None:
                lines.append(f"  Last duration: {s['last_duration_ms']}ms")
            if s["last_run_at"]:
                lines.append(f"  Last run: {s['last_run_at'].strftime('%Y-%m-%d %H:%M UTC')}")
            if s["last_error"]:
                err = s["last_error"][:100]
                lines.append(f"  Error: {_esc(err)}")
            lines.append("")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_scheduler crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Scheduler error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        service = get_health_service()
        system = service.check_all()
        text = service.format_health_command(system)
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_health crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Health error: {e}", parse_mode=None)
        except Exception:
            pass


ASSET_ALIASES = {
    "BTC": "BTC/USD", "ETH": "ETH/USD", "XRP": "XRP/USD",
    "LINK": "LINK/USD", "LTC": "LTC/USD", "SOL": "SOL/USD",
    "DOGE": "DOGE/USD", "AVAX": "AVAX/USD", "DOT": "DOT/USD",
    "ADA": "ADA/USD", "TRX": "TRX/USD", "SUI": "SUI/USD",
    "NEAR": "NEAR/USD", "ATOM": "ATOM/USD", "UNI": "UNI/USD",
    "AAVE": "AAVE/USD", "OP": "OP/USD", "HBAR": "HBAR/USD",
    "ETC": "ETC/USD", "ARB": "ARB/USD",
    "FIL": "FIL/USD", "APT": "APT/USD", "POL": "POL/USD",
    "ALGO": "ALGO/USD", "INJ": "INJ/USD", "RENDER": "RENDER/USD",
    "STX": "STX/USD", "LDO": "LDO/USD", "CRV": "CRV/USD",
    "ZEC": "ZEC/USD", "ONDO": "ONDO/USD",
    "JTO": "JTO/USD", "TIA": "TIA/USD", "WLD": "WLD/USD",
    "TAO": "TAO/USD", "JUP": "JUP/USD", "HYPE": "HYPE/USD",
    "GRASS": "GRASS/USD",
}


@owner_only
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if args:
            query = args[0].upper()
            symbol = ASSET_ALIASES.get(query, query if "/" in query else None)
            if symbol:
                targets = [a for a in settings.assets if a.symbol == symbol]
            else:
                await update.message.reply_text(
                    f"Unknown asset: {query}\nUse: BTC, ETH, XRP, LINK, LTC",
                )
                return
        else:
            targets = list(settings.assets)

        pipeline = get_pipeline()

        for asset in targets:
            text = await _debug_asset(pipeline, asset)
            await update.message.reply_text(text, parse_mode=None)
    except Exception as e:
        logger.error("cmd_debug crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Debug error: {e}", parse_mode=None)
        except Exception:
            pass


async def _debug_asset(pipeline, asset) -> str:
    import numpy as np

    lines = [f"\U0001f50d {asset.symbol}", ""]

    safety = await pipeline.get_analysis_ready_data(asset)

    if not safety.safe:
        lines.append(f"Provider: {safety.provider_used}")
        lines.append(f"Status: DATA UNAVAILABLE")
        lines.append(f"Reason: {safety.reason}")
        return "\n".join(lines)

    lines.append(f"Provider: {safety.provider_used}")
    lines.append(f"Price: ${safety.current_price:,.2f}")
    lines.append("")

    raw_df = safety.daily_df
    n_candles = len(raw_df)
    enriched = compute_indicators(raw_df)
    latest = enriched.iloc[-1]
    prev = enriched.iloc[-2] if len(enriched) > 1 else latest

    first_ts = str(enriched.iloc[0].get("open_time", "?"))
    last_ts = str(enriched.iloc[-1].get("open_time", "?"))

    lines.append("━ Data Quality")
    lines.append(f"Candles: {n_candles}")
    lines.append(f"First: {first_ts}")
    lines.append(f"Last:  {last_ts}")

    if "open_time" in enriched.columns:
        import pandas as pd
        times = pd.to_datetime(enriched["open_time"], errors="coerce").dropna()
        if len(times) > 1:
            diffs = times.diff().dropna()
            median_gap = diffs.median()
            big_gaps = diffs[diffs > median_gap * 2]
            if len(big_gaps) > 0:
                lines.append(f"Gaps detected: {len(big_gaps)}")
                worst = big_gaps.max()
                lines.append(f"Largest gap: {worst}")
            else:
                lines.append("Gaps: none")

    warmup = indicator_warmup_status(n_candles)
    not_ready = [name for name, ok in warmup.items() if not ok]
    if not_ready:
        lines.append(f"Warm-up incomplete: {', '.join(not_ready)}")
    else:
        lines.append("Warm-up: all indicators valid")

    nan_fields = regime_nan_fields(latest)
    if nan_fields:
        lines.append(f"NaN regime inputs: {', '.join(nan_fields)}")
    else:
        lines.append("NaN regime inputs: none")
    lines.append("")

    def _fv(val):
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return "NaN"
        return val

    ema50 = _fv(latest.get("ema50"))
    ema200 = _fv(latest.get("ema200"))
    er20 = _fv(latest.get("er20"))
    adx14 = _fv(latest.get("adx14"))
    rvol = _fv(latest.get("rvol"))
    rvol_median = _fv(latest.get("rvol_median_252"))
    rvol_pct25 = _fv(latest.get("rvol_pct25"))
    p48h = _fv(latest.get("price_change_48h"))
    p_short = _fv(latest.get("price_change_short"))
    close = float(latest.get("close", 0))
    prev_close = float(prev.get("close", 0))
    prev_ema50_v = prev.get("ema50")
    prev_ema50 = float(prev_ema50_v) if prev_ema50_v is not None and not (isinstance(prev_ema50_v, float) and np.isnan(prev_ema50_v)) else 0.0

    lines.append("━ Indicators")
    lines.append(f"EMA50: {ema50 if ema50 == 'NaN' else f'{ema50:,.2f}'}")
    lines.append(f"EMA200: {ema200 if ema200 == 'NaN' else f'{ema200:,.2f}'}")
    lines.append(f"ER20: {er20 if er20 == 'NaN' else f'{er20:.4f}'}")
    lines.append(f"ADX14: {adx14 if adx14 == 'NaN' else f'{adx14:.1f}'}")
    lines.append(f"RVol: {rvol if rvol == 'NaN' else f'{rvol:.4f}'}  (med: {rvol_median if rvol_median == 'NaN' else f'{rvol_median:.4f}'})")
    lines.append(f"48h change: {p48h if p48h == 'NaN' else f'{p48h:+.2%}'}")
    lines.append("")

    def check(ok): return "✅" if ok else "❌"

    regime = classify_regime(latest)

    if regime == MarketRegime.DATA_INSUFFICIENT:
        lines.append(f"━ Regime: {regime.value}")
        lines.append(f"Cannot classify — NaN in: {', '.join(nan_fields)}")
        lines.append("")
        lines.append("━ Signal: NO_TRADE")
        lines.append("Reason: Data insufficient for regime classification")
        return "\n".join(lines)

    rvol_f = float(rvol)
    rvol_median_f = float(rvol_median)
    rvol_pct25_f = float(rvol_pct25)
    er20_f = float(er20)
    p48h_f = float(p48h)
    p_short_f = float(p_short)
    ema50_f = float(ema50)
    ema200_f = float(ema200)

    lines.append("━ PANIC conditions")
    c_panic_drop = p48h_f <= -0.10
    c_panic_vol = rvol_median_f > 0 and rvol_f > 1.8 * rvol_median_f
    lines.append(f"  48h drop ≤ -10%    {check(c_panic_drop)}  ({p48h_f:+.2%})")
    lines.append(f"  RVol > 1.8×median  {check(c_panic_vol)}  ({rvol_f:.4f} vs {1.8*rvol_median_f:.4f})")
    lines.append("")

    lines.append("━ LOWVOL conditions")
    c_lv_vol = rvol_pct25_f > 0 and rvol_f <= rvol_pct25_f
    c_lv_er = er20_f < 0.35
    lines.append(f"  RVol ≤ pct25       {check(c_lv_vol)}  ({rvol_f:.4f} vs {rvol_pct25_f:.4f})")
    lines.append(f"  ER20 < 0.35        {check(c_lv_er)}  ({er20_f:.4f})")
    lines.append("")

    lines.append("━ TREND conditions")
    c_tr_er = er20_f >= 0.35
    c_tr_price = close > ema200_f
    c_tr_ema = ema50_f > ema200_f
    lines.append(f"  ER20 ≥ 0.35        {check(c_tr_er)}  ({er20_f:.4f})")
    lines.append(f"  Price > EMA200     {check(c_tr_price)}  ({close:,.2f} vs {ema200_f:,.2f})")
    lines.append(f"  EMA50 > EMA200     {check(c_tr_ema)}  ({ema50_f:,.2f} vs {ema200_f:,.2f})")
    lines.append("")

    lines.append(f"━ Regime: {regime.value}")

    if regime == MarketRegime.CHOP:
        failing = []
        if not c_tr_er:
            failing.append("ER20 below 0.35")
        if not c_tr_price:
            failing.append("Price below EMA200")
        if not c_tr_ema:
            failing.append("EMA50 below EMA200")
        lines.append(f"TREND blocked by: {', '.join(failing)}")

    lines.append("")
    lines.append("━ BUY gate checks")
    portfolio = get_portfolio()
    prices = await get_live_prices()
    equity = portfolio.get_total_equity(prices)
    open_pos = portfolio.get_open_positions()
    existing = [p for p in open_pos if p.get("symbol") == asset.symbol]

    c_not_panic = regime != MarketRegime.PANIC
    critical_level = settings.loss_level + 5
    preserve_level = settings.win_level - 10
    c_balance_ok = critical_level < equity < preserve_level
    c_above_ema200 = safety.current_price > ema200_f if ema200_f > 0 else False
    c_candle_conf = prev_close > prev_ema50 if prev_close and prev_ema50 else False
    c_no_spike = abs(p_short_f) <= 0.08
    c_max_pos = len([p for p in open_pos if p.get("status") == "open"]) < settings.max_open_positions

    lines.append(f"  Not PANIC          {check(c_not_panic)}")
    lines.append(f"  Equity {critical_level:.0f}-{preserve_level:.0f}    {check(c_balance_ok)}  (${equity:.2f})")
    lines.append(f"  Price > EMA200     {check(c_above_ema200)}")
    lines.append(f"  Candle confirm     {check(c_candle_conf)}  (prev close {prev_close:,.2f} vs prev EMA50 {prev_ema50:,.2f})")
    lines.append(f"  No spike (≤8%)     {check(c_no_spike)}  ({p_short_f:+.2%})")
    lines.append(f"  Open positions <{settings.max_open_positions}  {check(c_max_pos)}  ({len(existing)} for this asset)")

    engine = StrategyEngine()
    signal = engine.analyze(
        asset.symbol, safety.daily_df, safety.daily_df, safety.current_price,
        equity, open_pos, portfolio.get_total_open_risk(),
    )
    lines.append("")
    lines.append(f"━ Signal: {signal.signal_type}")
    lines.append(f"Reason: {signal.reason}")

    return "\n".join(lines)


@owner_only
async def cmd_reset_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        result = portfolio.reset_challenge_status()
        await update.message.reply_text(f"\U0001f504 {result}", parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_reset_challenge crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Reset challenge error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_new_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        archive, msg = portfolio.start_new_challenge()
        clear_last_signals()

        try:
            with get_session() as session:
                session.add(AuditLog(
                    action="NEW_CHALLENGE",
                    actor="owner",
                    detail=archive,
                ))
        except Exception as e:
            logger.error("Failed to persist challenge archive: %s", e)

        record_portfolio_snapshot("new_challenge")
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error("cmd_new_challenge crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"New challenge error: {e}", parse_mode=None)
        except Exception:
            pass



@owner_only
async def cmd_rebalance_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.strategy.rebalance import (
        find_rebalance_candidates, compute_5d_momentum,
        generate_rebalance_suggestion, format_rebalance_message,
        EXPERIMENTAL_WARNING,
    )
    try:
        portfolio = get_portfolio()
        prices = await get_live_prices()
        open_positions = portfolio.get_open_positions()

        if not open_positions:
            await update.message.reply_text("No open positions to rebalance.", parse_mode=None)
            return

        candidates = find_rebalance_candidates(open_positions, prices)
        if not candidates:
            await update.message.reply_text(
                "No positions near stop-loss (70%+ of entry-to-stop distance). "
                "Rebalance not triggered.",
                parse_mode=None,
            )
            return

        pipeline = get_pipeline()
        daily_dfs: dict[str, pd.DataFrame] = {}
        active_assets = [a for a in settings.assets if a.active]
        for asset in active_assets:
            try:
                safety = await pipeline.get_analysis_ready_data(asset)
                if safety.safe and safety.daily_df is not None:
                    daily_dfs[asset.symbol] = safety.daily_df
            except Exception:
                logger.warning("Failed to fetch data for %s in rebalance momentum", asset.symbol, exc_info=True)

        momentum = compute_5d_momentum(daily_dfs)
        total_risk = portfolio.get_total_open_risk()

        for candidate in candidates:
            suggestion = generate_rebalance_suggestion(
                candidate, momentum, total_risk, portfolio.balance_usd,
            )
            if suggestion:
                if suggestion.buy_symbol in prices:
                    suggestion.buy_price = prices[suggestion.buy_symbol]
                msg = format_rebalance_message(suggestion)
                await update.message.reply_text(msg, parse_mode="Markdown")

                with get_session() as session:
                    lifecycle = SignalLifecycle(session)
                    asset = session.query(Asset).filter(
                        Asset.symbol == candidate["symbol"]
                    ).first()
                    if asset:
                        from datetime import timezone as tz
                        from datetime import timedelta
                        lifecycle.create_signal(
                            asset_id=asset.id,
                            signal_type="PARTIAL_REBALANCE_SELL",
                            regime="EXPERIMENTAL",
                            expires_at=datetime.now(tz.utc) + timedelta(hours=2),
                            reason="Partial rebalance — 70% to stop-loss reached",
                            entry_price=candidate["current_price"],
                            position_size_usd=suggestion.sell_value_usd,
                            explanation=EXPERIMENTAL_WARNING,
                            priority="MEDIUM",
                        )
            else:
                await update.message.reply_text(
                    f"Position {candidate['symbol']} qualifies but no positive-momentum "
                    f"alternative found among tracked assets.",
                    parse_mode=None,
                )

    except Exception as e:
        logger.error("cmd_rebalance_suggestion crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Rebalance error: {e}", parse_mode=None)
        except Exception:
            pass


def _normalize_symbol(raw: str) -> str | None:
    cleaned = raw.strip().strip("[]").upper()
    if not cleaned:
        return None
    if "/" in cleaned:
        return cleaned
    return f"{cleaned}/USD"


@owner_only
async def cmd_manual_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args or []
        if len(args) < 2 or len(args) > 3:
            await update.message.reply_text(
                "Usage: /manual\\_buy TICKER AMOUNT \\[@price\\]\n"
                "Example: /manual\\_buy BTC 200\n"
                "Example: /manual\\_buy ZEC 10000 @840.50\n\n"
                "Records a buy you already made in Kraken.\n"
                "Optional @price uses your real execution price instead of live market.",
                parse_mode="Markdown",
            )
            return

        symbol = _normalize_symbol(args[0])
        if not symbol:
            await update.message.reply_text(
                "Could not parse ticker. Use: /manual_buy BTC 200 (no brackets)",
                parse_mode=None,
            )
            return

        raw_amount = args[1].strip().strip("[]")
        try:
            amount = float(raw_amount)
        except ValueError:
            await update.message.reply_text(
                f"'{args[1]}' is not a valid number. Use: /manual_buy BTC 200",
                parse_mode=None,
            )
            return

        if amount <= 0:
            await update.message.reply_text("Amount must be positive.", parse_mode=None)
            return

        override_price: float | None = None
        if len(args) == 3:
            raw_price = args[2].strip().lstrip("@")
            try:
                override_price = float(raw_price)
                if override_price <= 0:
                    await update.message.reply_text("Price must be positive.", parse_mode=None)
                    return
            except ValueError:
                await update.message.reply_text(
                    f"'{args[2]}' is not a valid price. Use: /manual_buy ZEC 10000 @840.50",
                    parse_mode=None,
                )
                return

        asset_cfg = next((a for a in settings.assets if a.symbol == symbol), None)
        if not asset_cfg:
            known = ", ".join(a.symbol.split("/")[0] for a in settings.assets[:10])
            await update.message.reply_text(
                f"Unknown asset '{symbol}'. Known tickers: {known}...",
                parse_mode=None,
            )
            return

        import asyncio
        try:
            prices = await asyncio.wait_for(get_live_prices([symbol]), timeout=30)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"Price fetch timed out for {symbol}. Try again in a minute.",
                parse_mode=None,
            )
            return

        if override_price:
            price = override_price
            price_source = "manual"
        elif symbol in prices:
            price = prices[symbol]
            price_source = "live"
        else:
            await update.message.reply_text(
                f"Could not fetch live price for {symbol}. "
                "Try again or specify price: /manual_buy ZEC 10000 @840",
                parse_mode=None,
            )
            return

        stop_loss_pct = float(asset_cfg.stop_loss_pct) if hasattr(asset_cfg, "stop_loss_pct") else 0.03
        stop_loss = price * (1 - stop_loss_pct)
        risk_dollars = amount * stop_loss_pct

        portfolio = get_portfolio()
        ok, msg = portfolio.confirm_buy(
            symbol=symbol,
            entry_price=price,
            position_value_usd=amount,
            stop_loss=stop_loss,
            risk_dollars=risk_dollars,
            prices=prices,
            force=True,
        )

        if ok:
            try:
                with get_session() as session:
                    session.add(AuditLog(
                        action="manual_buy",
                        actor=str(update.effective_user.id),
                        detail={"symbol": symbol, "amount": amount, "price": price, "price_source": price_source},
                    ))
            except Exception as e:
                logger.error("Failed to write manual_buy audit log: %s", e)
            record_portfolio_snapshot("manual_buy", prices)

        price_note = f" (manual)" if price_source == "manual" else ""
        await update.message.reply_text(
            f"{'Done' if ok else 'Failed'}: {msg}\n"
            f"Price: ${price:,.2f}{price_note} | Stop: ${stop_loss:,.2f}",
            parse_mode=None,
        )

    except Exception as e:
        logger.error("cmd_manual_buy crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_manual_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args or []
        if len(args) != 1:
            await update.message.reply_text(
                "Usage: /manual_sell TICKER\n"
                "Example: /manual_sell BTC\n\n"
                "Records a sell you already made in Kraken at current market price.",
                parse_mode=None,
            )
            return

        symbol = _normalize_symbol(args[0])
        if not symbol:
            await update.message.reply_text(
                "Could not parse ticker. Use: /manual_sell BTC (no brackets)",
                parse_mode=None,
            )
            return

        portfolio = get_portfolio()
        open_pos = [p for p in portfolio.positions if p.status == "open" and p.symbol == symbol]
        if not open_pos:
            await update.message.reply_text(
                f"No open position for {symbol}. Nothing to sell.",
                parse_mode=None,
            )
            return

        import asyncio
        try:
            prices = await asyncio.wait_for(get_live_prices([symbol]), timeout=30)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"Price fetch timed out for {symbol}. Try again in a minute.",
                parse_mode=None,
            )
            return

        if symbol not in prices:
            await update.message.reply_text(
                f"Could not fetch live price for {symbol}. Try again in a minute.",
                parse_mode=None,
            )
            return

        price = prices[symbol]
        ok, msg = portfolio.confirm_sell(
            symbol=symbol,
            exit_price=price,
            prices=prices,
        )

        if ok:
            try:
                with get_session() as session:
                    session.add(AuditLog(
                        action="manual_sell",
                        actor=str(update.effective_user.id),
                        detail={"symbol": symbol, "price": price},
                    ))
            except Exception as e:
                logger.error("Failed to write manual_sell audit log: %s", e)
            record_portfolio_snapshot("manual_sell", prices)

        await update.message.reply_text(
            f"{'Done' if ok else 'Failed'}: {msg}\nPrice: ${price:,.2f}",
            parse_mode=None,
        )

    except Exception as e:
        logger.error("cmd_manual_sell crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_switch_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args or []
        if len(args) != 1 or args[0].lower() not in ("live", "paper"):
            await update.message.reply_text(
                "Usage: /switch\\_mode live | paper\n\n"
                "Switches the active portfolio between PAPER and LIVE modes.\n"
                "Data is fully isolated — each mode has its own positions and balance.",
                parse_mode="Markdown",
            )
            return

        target = args[0].lower()
        new_mode = "LIVE_FUNDED" if target == "live" else "PAPER_CHALLENGE"
        old_mode = get_active_mode()

        if new_mode == old_mode:
            label = "LIVE" if new_mode == "LIVE_FUNDED" else "PAPER"
            await update.message.reply_text(
                f"Already in {label} mode.", parse_mode=None,
            )
            return

        set_active_mode(new_mode)

        if new_mode == "LIVE_FUNDED":
            settings.agent_mode = AgentMode.LIVE_FUNDED
        else:
            settings.agent_mode = AgentMode.PAPER_CHALLENGE

        try:
            from src.database.repository import AppSettingRepository
            with get_session() as session:
                repo = AppSettingRepository(session)
                repo.set("active_portfolio_mode", new_mode)
                repo.set("agent_mode", settings.agent_mode.value)
        except Exception as e:
            logger.error("Failed to persist mode switch: %s", e)

        portfolio = get_portfolio()
        label = "LIVE" if new_mode == "LIVE_FUNDED" else "PAPER"
        await update.message.reply_text(
            f"Switched to {label} mode.\n"
            f"Balance: ${portfolio.balance_usd:.2f}\n"
            f"Open positions: {len([p for p in portfolio.positions if p.status == 'open'])}\n"
            f"Status: {portfolio.challenge_status}",
            parse_mode=None,
        )

        try:
            with get_session() as session:
                session.add(AuditLog(
                    action="switch_mode",
                    actor=str(update.effective_user.id),
                    detail={"from": old_mode, "to": new_mode},
                ))
        except Exception as e:
            logger.error("Failed to write switch_mode audit log: %s", e)

    except Exception as e:
        logger.error("cmd_switch_mode crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_sync_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args or []
        if len(args) != 3:
            await update.message.reply_text(
                "Usage: /sync\\_portfolio TICKER QUANTITY CASH\n"
                "Example: /sync\\_portfolio ZEC 12.31281 0\n\n"
                "Replaces current portfolio state with actual exchange state.\n"
                "Use after executing trades on Kraken to sync the bot.",
                parse_mode="Markdown",
            )
            return

        symbol = _normalize_symbol(args[0])
        if not symbol:
            await update.message.reply_text(
                "Could not parse ticker.", parse_mode=None,
            )
            return

        try:
            quantity = float(args[1])
        except ValueError:
            await update.message.reply_text(
                f"'{args[1]}' is not a valid quantity.", parse_mode=None,
            )
            return

        try:
            cash = float(args[2])
        except ValueError:
            await update.message.reply_text(
                f"'{args[2]}' is not a valid cash amount.", parse_mode=None,
            )
            return

        if quantity < 0 or cash < 0:
            await update.message.reply_text(
                "Quantity and cash must be non-negative.", parse_mode=None,
            )
            return

        asset_cfg = next((a for a in settings.assets if a.symbol == symbol), None)
        if not asset_cfg:
            known = ", ".join(a.symbol.split("/")[0] for a in settings.assets[:10])
            await update.message.reply_text(
                f"Unknown asset '{symbol}'. Known: {known}...", parse_mode=None,
            )
            return

        import asyncio
        try:
            prices = await asyncio.wait_for(get_live_prices([symbol]), timeout=30)
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"Price fetch timed out for {symbol}.", parse_mode=None,
            )
            return

        if symbol not in prices:
            await update.message.reply_text(
                f"Could not fetch live price for {symbol}.", parse_mode=None,
            )
            return

        current_price = prices[symbol]
        portfolio = get_portfolio()
        result = portfolio.sync_portfolio(
            symbol=symbol,
            quantity=quantity,
            cash=cash,
            current_price=current_price,
        )

        equity = portfolio.get_total_equity(prices)
        record_portfolio_snapshot("sync_portfolio", prices)

        await update.message.reply_text(
            f"{result}\n"
            f"Challenge: {portfolio.challenge_status}\n"
            f"Distance to win: ${settings.win_level - equity:.2f}\n"
            f"Distance to loss: ${equity - settings.loss_level:.2f}",
            parse_mode=None,
        )

        try:
            with get_session() as session:
                session.add(AuditLog(
                    action="sync_portfolio",
                    actor=str(update.effective_user.id),
                    detail={
                        "symbol": symbol, "quantity": quantity, "cash": cash,
                        "price": current_price, "equity": round(equity, 2),
                        "mode": get_active_mode(),
                    },
                ))
        except Exception as e:
            logger.error("Failed to write sync_portfolio audit log: %s", e)

    except Exception as e:
        logger.error("cmd_sync_portfolio crashed: %s", e, exc_info=True)
        try:
            await update.message.reply_text(f"Error: {e}", parse_mode=None)
        except Exception:
            pass


@owner_only
async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("CALENDAR_HANDLER_CALLED", flush=True)
    try:
        manager = get_calendar_manager()
        events = manager.get_upcoming(hours_ahead=48)

        if not events:
            try:
                await manager.refresh_events()
                events = manager.get_upcoming(hours_ahead=48)
            except Exception as refresh_err:
                logger.warning("Calendar refresh on first /calendar failed: %s", refresh_err)

        text = format_calendar_view(events)

        missing_keys = []
        import os
        if not os.getenv("JBLANKED_API_KEY"):
            missing_keys.append("JBLANKED_API_KEY")
        if not os.getenv("COINMARKETCAL_API_KEY"):
            missing_keys.append("COINMARKETCAL_API_KEY")
        if missing_keys:
            text += (
                "\n\nℹ️ External calendar sources unavailable — "
                f"missing env vars: {', '.join(missing_keys)}. "
                "Only hardcoded FOMC dates are shown."
            )

        await update.message.reply_text(text, parse_mode=None)
        print("CALENDAR_REPLY_SENT", flush=True)
    except BaseException as e:
        import traceback
        print(f"CALENDAR_ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        try:
            await update.message.reply_text(f"Error loading calendar: {e}", parse_mode=None)
        except Exception:
            logger.error("cmd_calendar fallback reply also failed: %s", e)
        raise


def create_bot(token: str | None = None) -> Application:
    bot_token = token or settings.telegram_bot_token
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    validate_auth_config()

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("scheduler", cmd_scheduler))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("reset_challenge", cmd_reset_challenge))
    app.add_handler(CommandHandler("new_challenge", cmd_new_challenge))
    app.add_handler(CommandHandler("rebalance_suggestion", cmd_rebalance_suggestion))
    app.add_handler(CommandHandler("manual_buy", cmd_manual_buy))
    app.add_handler(CommandHandler("manual_sell", cmd_manual_sell))
    app.add_handler(CommandHandler("switch_mode", cmd_switch_mode))
    app.add_handler(CommandHandler("sync_portfolio", cmd_sync_portfolio))
    app.add_handler(CommandHandler("calendar", cmd_calendar))

    return app
