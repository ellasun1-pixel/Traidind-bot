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
from src.scheduler.jobs import get_portfolio, get_last_signals, get_scheduler_status
from src.notifier.formatter import SignalFormatter
from src.database import get_session, AuditLog
from src.auth.owner import owner_only, validate_auth_config
from src.auth.permissions import Permission, get_user_permissions
from src.database.session import check_db_health
from src.health.service import get_health_service

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
        "/help — Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = get_portfolio()
    last_signals = get_last_signals()

    status_lines = [
        "\U0001f4ca *Status*",
        "",
        f"Mode: {settings.agent_mode.value}",
        f"Balance: ${portfolio.balance_usd:.2f}",
        f"Challenge: {portfolio.challenge_status.upper()}",
        f"Open positions: {len([p for p in portfolio.positions if p.status == 'open'])}",
        "",
    ]

    if last_signals:
        status_lines.append("*Latest regimes:*")
        for symbol, sig in last_signals.items():
            status_lines.append(f"  {symbol}: {sig.regime.value} → {sig.signal_type}")
    else:
        status_lines.append("_No signals generated yet._")

    await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown")


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

    try:
        with get_session() as session:
            session.add(AuditLog(action="CONFIRM", actor="owner", detail={"results": results}))
    except Exception:
        pass

    await update.message.reply_text(
        "\U0001f4cb *Confirmation Results*\n\n" + "\n".join(results),
        parse_mode="Markdown",
    )


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

    return app
