from __future__ import annotations

import json
import logging
import os
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


def run_bot():
    from src.config import settings
    from src.database import init_db, check_db_health
    from src.telegram_bot.bot import create_bot
    from src.scheduler.jobs import (
        setup_scheduler, set_send_message_func, startup_sweep, market_check_job,
    )
    from src.auth.owner import validate_auth_config
    from src.health.service import get_health_service

    logger.info("=== STARTUP DIAGNOSTICS ===")

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

    init_db()
    logger.info("[4/8] Migrations: Applied")

    try:
        validate_auth_config()
        logger.info("[5/8] Authentication: Validated")
    except Exception as e:
        logger.error("[5/8] Authentication FAILED: %s", e)
        sys.exit(1)

    app = create_bot()
    logger.info("[6/8] Telegram bot: Created")

    scheduler = setup_scheduler()
    logger.info("[7/8] Scheduler: Initialized (6 jobs)")

    logger.info("[8/8] Configuration: %d assets, %d min interval, strategy v%s",
                len(settings.assets), settings.check_interval_minutes,
                settings.strategy_version)

    async def send_to_chat(text: str):
        try:
            bot = app.bot
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if chat_id:
                await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Failed to send message: %s", e)

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
        logger.info("=== STARTUP COMPLETE — readiness gate open ===")

        try:
            await market_check_job()
            logger.info("Initial market check completed")
        except Exception as e:
            logger.error("Initial market check failed: %s", e)

    app.post_init = post_init

    app.run_polling(drop_pending_updates=True)


def run_web():
    os.system(f"streamlit run {Path(__file__).parent / 'src' / 'web_panel' / 'app.py'} --server.port 8501")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        run_web()
    else:
        run_bot()
