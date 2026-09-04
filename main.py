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


def _ensure_schema_columns():
    """Ensure every column the ORM expects actually exists in the DB.

    Alembic migrations sometimes advance alembic_version without the
    ALTER TABLE actually succeeding, leaving the DB schema behind the
    code.  This function compares every model's columns against the
    real DB and adds anything missing.  Runs before ORM init, idempotent.
    """
    from sqlalchemy import create_engine, text, inspect as sa_inspect

    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        logger.warning("DATABASE_URL not set, skipping schema guard")
        return

    EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
        "paper_account": {
            "agent_mode": "VARCHAR(20) NOT NULL DEFAULT 'PAPER_CHALLENGE'",
            "balance_usd": "NUMERIC(12,2) NOT NULL DEFAULT 10000.00",
            "peak_balance": "NUMERIC(12,2) NOT NULL DEFAULT 10000.00",
            "starting_balance": "NUMERIC(12,2) NOT NULL DEFAULT 10000.00",
            "realized_pnl": "NUMERIC(12,2) NOT NULL DEFAULT 0.00",
            "daily_loss": "NUMERIC(12,2) NOT NULL DEFAULT 0.00",
            "daily_loss_date": "DATE NOT NULL DEFAULT CURRENT_DATE",
            "challenge_status": "VARCHAR(10) NOT NULL DEFAULT 'active'",
            "strategy_version": "VARCHAR(20) NOT NULL DEFAULT '1.0'",
            "updated_at": "TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        },
        "paper_positions": {
            "agent_mode": "VARCHAR(20) NOT NULL DEFAULT 'PAPER_CHALLENGE'",
            "peak_price": "NUMERIC(18,8)",
            "tp1_fired": "BOOLEAN NOT NULL DEFAULT false",
            "tp2_fired": "BOOLEAN NOT NULL DEFAULT false",
            "signal_id": "VARCHAR(36)",
            "take_profit": "NUMERIC(18,8)",
            "close_reason": "VARCHAR(20)",
        },
        "portfolio_snapshots": {
            "agent_mode": "VARCHAR(20)",
        },
        "daily_snapshots": {
            "agent_mode": "VARCHAR(20) NOT NULL DEFAULT 'PAPER_CHALLENGE'",
            "peak_balance": "NUMERIC(12,2) NOT NULL DEFAULT 10000.00",
            "strategy_version": "VARCHAR(20) NOT NULL DEFAULT '1.0'",
        },
        "signals": {
            "price_range_low": "NUMERIC(18,8)",
            "price_range_high": "NUMERIC(18,8)",
            "price_tolerance_pct": "NUMERIC(6,4) DEFAULT 0.02",
            "superseded_at": "TIMESTAMPTZ",
            "superseded_reason": "TEXT",
            "previous_signal_id": "VARCHAR(36)",
        },
        "scheduler_state": {
            "success_count": "INTEGER NOT NULL DEFAULT 0",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_duration_ms": "INTEGER",
            "last_completed_at": "TIMESTAMPTZ",
            "last_started_at": "TIMESTAMPTZ",
        },
        "market_data_meta": {
            "valid_candle_count": "INTEGER",
            "validation_error": "TEXT",
        },
        "health_transitions": {
            "recovered_at": "TIMESTAMPTZ",
            "recovery_seconds": "INTEGER",
        },
    }

    RENAME_COLUMNS: dict[str, dict[str, str]] = {
        "paper_account": {"mode": "agent_mode"},
        "paper_positions": {"mode": "agent_mode"},
    }

    engine = create_engine(db_url)
    added_count = 0
    try:
        with engine.connect() as conn:
            insp = sa_inspect(conn)
            existing_tables = set(insp.get_table_names())

            for table, columns in EXPECTED_COLUMNS.items():
                if table not in existing_tables:
                    logger.info("SCHEMA_GUARD %s: table not found, will be created by init_db", table)
                    continue

                col_names = {c["name"] for c in insp.get_columns(table)}

                renames = RENAME_COLUMNS.get(table, {})
                for old_name, new_name in renames.items():
                    if old_name in col_names and new_name not in col_names:
                        logger.warning("SCHEMA_GUARD %s: renaming %s -> %s", table, old_name, new_name)
                        conn.execute(text(
                            f'ALTER TABLE {table} RENAME COLUMN "{old_name}" TO {new_name}'
                        ))
                        conn.commit()
                        col_names.discard(old_name)
                        col_names.add(new_name)

                for col, col_def in columns.items():
                    if col not in col_names:
                        logger.warning("SCHEMA_GUARD %s: adding missing column %s", table, col)
                        conn.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"
                        ))
                        conn.commit()
                        added_count += 1

            # --- Index guard: replace uq_one_open_per_asset with mode-aware version ---
            if "paper_positions" in existing_tables:
                idx_rows = conn.execute(text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'paper_positions' AND indexname IN "
                    "('uq_one_open_per_asset', 'uq_one_open_per_asset_mode')"
                )).fetchall()
                idx_names = {r[0] for r in idx_rows}

                if "uq_one_open_per_asset" in idx_names and "uq_one_open_per_asset_mode" not in idx_names:
                    logger.warning("SCHEMA_GUARD: replacing uq_one_open_per_asset with mode-aware version")
                    conn.execute(text("DROP INDEX IF EXISTS uq_one_open_per_asset"))
                    conn.execute(text(
                        "CREATE UNIQUE INDEX uq_one_open_per_asset_mode "
                        "ON paper_positions (asset_id, agent_mode) "
                        "WHERE is_open = true"
                    ))
                    conn.commit()
                    logger.warning("SCHEMA_GUARD: uq_one_open_per_asset_mode created")
                elif "uq_one_open_per_asset" not in idx_names and "uq_one_open_per_asset_mode" not in idx_names:
                    logger.warning("SCHEMA_GUARD: creating missing uq_one_open_per_asset_mode")
                    conn.execute(text(
                        "CREATE UNIQUE INDEX uq_one_open_per_asset_mode "
                        "ON paper_positions (asset_id, agent_mode) "
                        "WHERE is_open = true"
                    ))
                    conn.commit()
                    logger.warning("SCHEMA_GUARD: uq_one_open_per_asset_mode created")
                else:
                    logger.info("SCHEMA_GUARD: uq_one_open_per_asset_mode already present")

            if added_count:
                logger.warning("SCHEMA_GUARD: added %d missing columns total", added_count)
            else:
                logger.info("SCHEMA_GUARD: all expected columns present")
    except Exception as e:
        logger.error("SCHEMA_GUARD failed: %s", e, exc_info=True)
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

    _ensure_schema_columns()

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
