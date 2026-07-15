import os
import sys

os.environ["TELEGRAM_BOT_TOKEN"] = "test_token_not_real"
os.environ["DATABASE_URL"] = "sqlite:///test_challenge.db"
os.environ["TELEGRAM_OWNER_IDS"] = "123456789"
os.environ["TELEGRAM_CHAT_IDS"] = "123456789"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
