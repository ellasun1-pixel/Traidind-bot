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
from src.scheduler.jobs import get_portfolio, get_last_signals, get_scheduler_status, get_pipeline, record_portfolio_snapshot
from src.notifier.formatter import SignalFormatter
from src.database import get_session, AuditLog
from src.auth.owner import owner_only, validate_auth_config
from src.auth.permissions import Permission, get_user_permissions
from src.database.session import check_db_health
from src.health.service import get_health_service
from src.strategy.indicators import compute_indicators, indicator_warmup_status, WARMUP
from src.strategy.regime import classify_regime, MarketRegime, regime_nan_fields
from src.strategy.engine import StrategyEngine

logger = logging.getLogger(__name__)

formatter = SignalFormatter()
_pending_signals: dict[int, dict] = {}
_next_signal_id = 1


def _store_pending_signal(signal_data: dict) -> int:
    global _next_signal_id
    sid = _next_signal_id
    _next_signal_id += 1
    _pending_signals[sid] = signal_data
    return sid


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
        "/new\\_challenge — Reset and start a new paper challenge\n"
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        portfolio = get_portfolio()
        last_signals = get_last_signals()
        equity = portfolio._get_equity_estimate()

        mode = settings.agent_mode.value.replace("_", "\\_")
        status_lines = [
            "\U0001f4ca *Status*",
            "",
            f"Mode: {mode}",
            f"Equity: ${equity:.2f}",
            f"Cash: ${portfolio.balance_usd:.2f}",
            f"Challenge: {portfolio.challenge_status.upper()}",
            f"Open positions: {len([p for p in portfolio.positions if p.status == 'open'])}",
            "",
        ]

        if last_signals:
            status_lines.append("*Latest regimes:*")
            for symbol, sig in last_signals.items():
                regime_val = sig.regime.value if sig.regime else "UNKNOWN"
                sig_type = sig.signal_type.replace("_", "\\_")
                status_lines.append(f"  {symbol}: {regime_val} - {sig_type}")
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
    portfolio = get_portfolio()
    prices = {}
    summary = portfolio.get_portfolio_summary(prices)
    text = formatter.format_portfolio_summary(summary)
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_signals = get_last_signals()
    if not last_signals:
        await update.message.reply_text("_No signals generated yet._", parse_mode="Markdown")
        return

    for symbol, sig in last_signals.items():
        text = formatter.format_signal(sig)
        await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


@owner_only
async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = get_portfolio()
    last_signals = get_last_signals()

    pending = {
        sym: sig for sym, sig in last_signals.items()
        if sig.signal_type in ("BUY", "SELL", "REDUCE", "TAKE_PROFIT", "MOVE_TO_USD")
    }

    if not pending:
        await update.message.reply_text("_No pending signals to confirm._", parse_mode="Markdown")
        return

    results = []
    challenge_ended = False
    for symbol, sig in pending.items():
        if sig.signal_type == "BUY":
            ok, msg = portfolio.confirm_buy(
                symbol=symbol,
                entry_price=sig.entry_price,
                position_value_usd=sig.position_size_usd,
                stop_loss=sig.stop_loss,
                risk_dollars=sig.max_loss_usd,
            )
            results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")
        elif sig.signal_type in ("SELL", "TAKE_PROFIT", "REDUCE", "MOVE_TO_USD"):
            ok, msg = portfolio.confirm_sell(
                symbol=symbol,
                exit_price=sig.entry_price,
            )
            results.append(f"{'✅' if ok else '❌'} {symbol}: {msg}")

        if not portfolio.is_challenge_active:
            challenge_ended = True

    try:
        with get_session() as session:
            session.add(AuditLog(action="CONFIRM", actor="owner", detail={"results": results}))
    except Exception:
        pass

    await update.message.reply_text(
        "\U0001f4cb *Confirmation Results*\n\n" + "\n".join(results),
        parse_mode="Markdown",
    )

    record_portfolio_snapshot("trade_confirm")

    if challenge_ended:
        ended_msg = portfolio.get_challenge_ended_message()
        await update.message.reply_text(ended_msg, parse_mode="Markdown")


@owner_only
async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    last_signals = get_last_signals()
    pending = {
        sym: sig for sym, sig in last_signals.items()
        if sig.signal_type in ("BUY", "SELL", "REDUCE", "TAKE_PROFIT", "MOVE_TO_USD")
    }
    if not pending:
        await update.message.reply_text("_No pending signals to reject._", parse_mode="Markdown")
        return

    rejected = list(pending.keys())
    for sym in rejected:
        del last_signals[sym]

    await update.message.reply_text(
        f"❌ Rejected signals for: {', '.join(rejected)}",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings.agent_mode = AgentMode.PAUSED
    await update.message.reply_text(
        "⏸️ Agent paused. Market observation continues but no signals will be sent.\n"
        "Use /resume to continue.",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings.agent_mode = AgentMode.PAPER_CHALLENGE
    await update.message.reply_text(
        "▶️ Agent resumed in PAPER_CHALLENGE mode.",
        parse_mode="Markdown",
    )


@owner_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and len(args) >= 2:
        key = args[0].lower()
        value = args[1].lower()
        if key == "beginner":
            settings.beginner_explanations = value in ("true", "1", "on", "yes")
            await update.message.reply_text(
                f"✅ BEGINNER_EXPLANATIONS = {settings.beginner_explanations}",
                parse_mode="Markdown",
            )
            return

    text = (
        "⚙️ *Settings*\n\n"
        f"Mode: {settings.agent_mode.value}\n"
        f"Beginner explanations: {settings.beginner_explanations}\n"
        f"Timezone: {settings.timezone}\n"
        f"Active hours: {settings.active_hours_start}:00–{settings.active_hours_end}:00\n"
        f"Check interval: {settings.check_interval_minutes} min\n"
        f"Assets: {', '.join(a.symbol for a in settings.assets)}\n"
        "\n_Toggle: /settings beginner true|false_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


@owner_only
async def cmd_scheduler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    statuses = get_scheduler_status()
    if not statuses:
        await update.message.reply_text("_No scheduler data yet._", parse_mode="Markdown")
        return

    lines = ["\U0001f553 *Scheduler Status*", ""]
    for s in statuses:
        status_emoji = "\U0001f7e2" if s["current_status"] == "idle" else "\U0001f7e1"
        if s["last_error"]:
            status_emoji = "\U0001f534"
        lines.append(f"{status_emoji} *{s['job_name']}*")
        lines.append(f"  Status: {s['current_status']}")
        lines.append(f"  Runs: {s['run_count']} (OK: {s['success_count']}, Fail: {s['failure_count']})")
        if s["last_duration_ms"] is not None:
            lines.append(f"  Last duration: {s['last_duration_ms']}ms")
        if s["last_run_at"]:
            lines.append(f"  Last run: {s['last_run_at'].strftime('%Y-%m-%d %H:%M UTC')}")
        if s["last_error"]:
            err = s["last_error"][:100]
            lines.append(f"  Error: {err}")
        lines.append("")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    service = get_health_service()
    system = service.check_all()
    text = service.format_health_command(system)
    await update.message.reply_text(text, parse_mode="Markdown")


ASSET_ALIASES = {
    "BTC": "BTC/USD", "ETH": "ETH/USD", "XRP": "XRP/USD",
    "LINK": "LINK/USD", "LTC": "LTC/USD", "SOL": "SOL/USD",
    "DOGE": "DOGE/USD", "AVAX": "AVAX/USD", "DOT": "DOT/USD",
}


@owner_only
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await update.message.reply_text(text, parse_mode="Markdown")


async def _debug_asset(pipeline, asset) -> str:
    import numpy as np

    lines = [f"\U0001f50d *{asset.symbol}*", ""]

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

    first_ts = enriched.iloc[0].get("open_time", "?")
    last_ts = enriched.iloc[-1].get("open_time", "?")

    lines.append("*Data Quality*")
    lines.append(f"Candles: {n_candles}")
    lines.append(f"First: {first_ts}")
    lines.append(f"Last:  {last_ts}")

    if "open_time" in enriched.columns:
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

    lines.append("*Indicators*")
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
        lines.append(f"*Regime: {regime.value}*")
        lines.append(f"Cannot classify — NaN in: {', '.join(nan_fields)}")
        lines.append("")
        lines.append("*Signal: NO\\_TRADE*")
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

    lines.append("*PANIC conditions*")
    c_panic_drop = p48h_f <= -0.10
    c_panic_vol = rvol_median_f > 0 and rvol_f > 1.8 * rvol_median_f
    lines.append(f"  48h drop ≤ -10%    {check(c_panic_drop)}  ({p48h_f:+.2%})")
    lines.append(f"  RVol > 1.8×median  {check(c_panic_vol)}  ({rvol_f:.4f} vs {1.8*rvol_median_f:.4f})")
    lines.append("")

    lines.append("*LOWVOL conditions*")
    c_lv_vol = rvol_pct25_f > 0 and rvol_f <= rvol_pct25_f
    c_lv_er = er20_f < 0.35
    lines.append(f"  RVol ≤ pct25       {check(c_lv_vol)}  ({rvol_f:.4f} vs {rvol_pct25_f:.4f})")
    lines.append(f"  ER20 < 0.35        {check(c_lv_er)}  ({er20_f:.4f})")
    lines.append("")

    lines.append("*TREND conditions*")
    c_tr_er = er20_f >= 0.35
    c_tr_price = close > ema200_f
    c_tr_ema = ema50_f > ema200_f
    lines.append(f"  ER20 ≥ 0.35        {check(c_tr_er)}  ({er20_f:.4f})")
    lines.append(f"  Price > EMA200     {check(c_tr_price)}  ({close:,.2f} vs {ema200_f:,.2f})")
    lines.append(f"  EMA50 > EMA200     {check(c_tr_ema)}  ({ema50_f:,.2f} vs {ema200_f:,.2f})")
    lines.append("")

    lines.append(f"*Regime: {regime.value}*")

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
    lines.append("*BUY gate checks*")
    portfolio = get_portfolio()
    equity = portfolio._get_equity_estimate()
    open_pos = portfolio.get_open_positions()
    existing = [p for p in open_pos if p.get("symbol") == asset.symbol]

    c_not_panic = regime != MarketRegime.PANIC
    c_balance_ok = 955 < equity < 1110
    c_above_ema200 = safety.current_price > ema200_f if ema200_f > 0 else False
    c_candle_conf = prev_close > prev_ema50 if prev_close and prev_ema50 else False
    c_no_spike = abs(p_short_f) <= 0.08
    c_max_pos = len([p for p in open_pos if p.get("status") == "open"]) < settings.max_open_positions

    lines.append(f"  Not PANIC          {check(c_not_panic)}")
    lines.append(f"  Equity 955-1110    {check(c_balance_ok)}  (${equity:.2f})")
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
    lines.append(f"*Signal: {signal.signal_type}*")
    lines.append(f"Reason: {signal.reason}")

    return "\n".join(lines)


@owner_only
async def cmd_reset_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = get_portfolio()
    result = portfolio.reset_challenge_status()
    await update.message.reply_text(f"\U0001f504 {result}", parse_mode="Markdown")


@owner_only
async def cmd_new_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = get_portfolio()
    archive, msg = portfolio.start_new_challenge()

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

    return app
