from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_app_ready = False
_ready_lock = threading.Lock()


def set_app_ready(ready: bool = True):
    global _app_ready
    with _ready_lock:
        _app_ready = ready


def is_app_ready() -> bool:
    with _ready_lock:
        return _app_ready


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            if is_app_ready():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ready"}).encode())
            else:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "initializing"}).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health server listening on 0.0.0.0:%d", port)


def _handle_sigterm(signum, frame):
    logger.warning("=== SIGTERM received — process shutting down (PID %d) ===", os.getpid())
    set_app_ready(False)
    sys.exit(0)


def _ensure_agent_mode_column():
    """Ensure 'mode' column is renamed to 'agent_mode' on both tables.

    Runs before ORM init so queries don't fail with UndefinedColumn.
    Idempotent — safe to run on every startup.
    """
    from sqlalchemy import create_engine, text, inspect as sa_inspect
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        logger.warning("DATABASE_URL not set, skipping column check")
        return
    engine = create_engine(db_url)
    with engine.connect() as conn:
        insp = sa_inspect(conn)
        for table in ("paper_account", "paper_positions"):
            try:
                col_names = [c["name"] for c in insp.get_columns(table)]
            except Exception:
                logger.info("COLUMN_CHECK %s: table not found, skipping", table)
                continue

            has_mode = "mode" in col_names
            has_agent_mode = "agent_mode" in col_names
            logger.info("COLUMN_CHECK %s: mode=%s, agent_mode=%s", table, has_mode, has_agent_mode)

            if has_agent_mode:
                logger.info("COLUMN_CHECK %s: agent_mode exists, OK", table)
            elif has_mode:
                logger.info("COLUMN_CHECK %s: renaming mode -> agent_mode", table)
                conn.execute(text(
                    f'ALTER TABLE {table} RENAME COLUMN "mode" TO agent_mode'
                ))
                conn.commit()
                logger.info("COLUMN_CHECK %s: rename DONE", table)
            else:
                logger.info("COLUMN_CHECK %s: adding agent_mode column", table)
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN agent_mode VARCHAR(20) "
                    f"NOT NULL DEFAULT 'PAPER_CHALLENGE'"
                ))
                conn.commit()
                logger.info("COLUMN_CHECK %s: added agent_mode DONE", table)
    engine.dispose()


def _ensure_position_tracking_columns():
    """Ensure peak_price, tp1_fired, tp2_fired exist on paper_positions.

    Migration 013 adds these, but if alembic_version advanced without the
    columns actually being created, every query on paper_positions crashes
    with UndefinedColumn.  This runs before ORM init and is idempotent.
    """
    from sqlalchemy import create_engine, text, inspect as sa_inspect
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        return
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            insp = sa_inspect(conn)
            try:
                col_names = [c["name"] for c in insp.get_columns("paper_positions")]
            except Exception:
                logger.info("COLUMN_CHECK paper_positions: table not found, skipping")
                return

            required = {
                "peak_price": "NUMERIC(18,8)",
                "tp1_fired": "BOOLEAN NOT NULL DEFAULT false",
                "tp2_fired": "BOOLEAN NOT NULL DEFAULT false",
            }
            for col, col_def in required.items():
                if col not in col_names:
                    logger.warning("COLUMN_CHECK paper_positions: adding missing column %s", col)
                    conn.execute(text(
                        f"ALTER TABLE paper_positions ADD COLUMN {col} {col_def}"
                    ))
                    conn.commit()
                    logger.info("COLUMN_CHECK paper_positions: %s added", col)
                else:
                    logger.info("COLUMN_CHECK paper_positions: %s exists, OK", col)
    finally:
        engine.dispose()


def run_bot():
    signal.signal(signal.SIGTERM, _handle_sigterm)

    from src.config import settings
    from src.database import init_db, check_db_health
    from src.telegram_bot.bot import create_bot
    from src.scheduler.jobs import (
        setup_scheduler, set_send_message_func, startup_sweep, market_check_job,
    )
    from src.auth.owner import validate_auth_config
    from src.health.service import get_health_service

    logger.info("=== STARTUP DIAGNOSTICS (PID %d) ===", os.getpid())

    logger.info("[1/8] Environment: %s", settings.app_env)
    logger.info("[1/8] Live trading: %s", settings.live_trading_enabled)
    if settings.live_trading_enabled:
        logger.error("LIVE_TRADING_ENABLED=true — aborting for safety")
        sys.exit(1)

    start_health_server()
    logger.info("[2/8] Health server started")

    db_health = check_db_health()
    if db_health["status"] != "ok":
        logger.error("[3/8] Database FAILED: %s", db_health.get("error", "unknown"))
        sys.exit(1)
    logger.info("[3/8] Database: Connected (%s)", db_health["backend"])

    _ensure_agent_mode_column()
    _ensure_position_tracking_columns()

    logger.info("[4/8] Starting schema verification...")
    try:
        init_db()
    except Exception as e:
        logger.error("[4/8] Schema verification FAILED: %s", e, exc_info=True)
        sys.exit(1)
    logger.info("[4/8] Schema verified")

    logger.info("[5/8] Validating auth config...")
    try:
        validate_auth_config()
    except Exception as e:
        logger.error("[5/8] Authentication FAILED: %s", e)
        sys.exit(1)
    logger.info("[5/8] Authentication: Validated")

    logger.info("[6/8] Creating Telegram bot...")
    app = create_bot()
    logger.info("[6/8] Telegram bot: Created")

    scheduler = setup_scheduler()
    logger.info("[7/8] Scheduler: Initialized (6 jobs)")

    logger.info("[8/8] Configuration: %d assets, %d min interval, strategy v%s",
                len(settings.assets), settings.check_interval_minutes,
                settings.strategy_version)

    _chat_id = settings.telegram_chat_id
    if not _chat_id and settings.telegram_chat_ids:
        _chat_id = settings.telegram_chat_ids.split(",")[0].strip()
    if not _chat_id:
        logger.error("No chat ID configured — set TELEGRAM_CHAT_ID or TELEGRAM_CHAT_IDS in environment")
    else:
        logger.info("Proactive notifications target chat: ***%s", _chat_id[-4:])

    async def send_to_chat(text: str):
        bot = app.bot
        if not _chat_id:
            logger.error("send_to_chat called but TELEGRAM_CHAT_ID is not set — message dropped")
            return
        try:
            await bot.send_message(chat_id=int(_chat_id), text=text, parse_mode="Markdown")
        except Exception:
            logger.warning("Markdown send failed, retrying as plain text")
            try:
                await bot.send_message(chat_id=int(_chat_id), text=text, parse_mode=None)
            except Exception as e:
                logger.error("Plain text send also failed: %s", e)
                raise

    set_send_message_func(send_to_chat)

    health_service = get_health_service()
    health_service.set_send_message_func(send_to_chat)

    async def post_init(application):
        await startup_sweep()

        scheduler.start()
        logger.info("Scheduler started (market check every %d min)", settings.check_interval_minutes)
        logger.info("Agent mode: %s", settings.agent_mode.value)
        logger.info("Active assets: %s", ", ".join(a.symbol for a in settings.assets))

        set_app_ready(True)
        logger.info("=== STARTUP COMPLETE — readiness gate open (PID %d) ===", os.getpid())

        try:
            from src.scheduler.jobs import get_active_mode
            mode = get_active_mode()
            await send_to_chat(
                f"Bot restarted and ready.\n"
                f"Active mode: {mode}\n"
                f"Commands are now being accepted."
            )
        except Exception as e:
            logger.warning("Failed to send startup notification: %s", e)

        try:
            await market_check_job()
            logger.info("Initial market check completed")
        except Exception as e:
            logger.error("Initial market check failed: %s", e)

    async def error_handler(update, context):
        logger.error(
            "=== UNHANDLED EXCEPTION in update handler === %s: %s",
            type(context.error).__name__, context.error, exc_info=context.error,
        )

    app.add_error_handler(error_handler)
    app.post_init = post_init

    logger.info("Starting polling loop (bootstrap_retries=-1, drop_pending_updates=False)")
    try:
        app.run_polling(
            drop_pending_updates=False,
            bootstrap_retries=-1,
        )
    except SystemExit:
        logger.warning("=== SystemExit in polling loop (PID %d) ===", os.getpid())
        raise
    except Exception as e:
        logger.critical("=== FATAL: polling loop crashed (PID %d) === %s: %s", os.getpid(), type(e).__name__, e, exc_info=True)
        sys.exit(1)
    finally:
        logger.warning("=== Polling loop exited (PID %d) ===", os.getpid())


def run_web():
    os.system(f"streamlit run {Path(__file__).parent / 'src' / 'web_panel' / 'app.py'} --server.port 8501")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        run_web()
    else:
        run_bot()
