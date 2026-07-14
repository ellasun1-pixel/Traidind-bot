from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)



def run_bot():
    from src.config import settings
    from src.database import init_db
    from src.telegram_bot.bot import create_bot
    from src.scheduler.jobs import setup_scheduler, set_send_message_func

    init_db()
    logger.info("Database initialized")

    app = create_bot()
    logger.info("Telegram bot created")

    scheduler = setup_scheduler()

    async def send_to_chat(text: str):
        try:
            bot = app.bot
            chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if chat_id:
                await bot.send_message(chat_id=int(chat_id), text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error("Failed to send message: %s", e)

    set_send_message_func(send_to_chat)

    async def post_init(application):
        scheduler.start()
        logger.info("Scheduler started (every %d minutes)", settings.check_interval_minutes)
        logger.info("Agent mode: %s", settings.agent_mode.value)
        logger.info("Active assets: %s", ", ".join(a.symbol for a in settings.assets))

    app.post_init = post_init

    app.run_polling(drop_pending_updates=True)


def run_web():
    os.system(f"streamlit run {Path(__file__).parent / 'src' / 'web_panel' / 'app.py'} --server.port 8501")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        run_web()
    else:
        run_bot()
