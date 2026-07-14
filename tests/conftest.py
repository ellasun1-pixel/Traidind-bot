import os
import sys

os.environ["TELEGRAM_BOT_TOKEN"] = "test_token_not_real"
os.environ["DATABASE_URL"] = "sqlite:///test_challenge.db"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
