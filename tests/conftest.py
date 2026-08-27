import os
import sys

os.environ["TELEGRAM_BOT_TOKEN"] = "test_token_not_real"
os.environ["DATABASE_URL"] = "sqlite:///test_challenge.db"
os.environ["TELEGRAM_OWNER_IDS"] = "123456789"
os.environ["TELEGRAM_CHAT_IDS"] = "123456789"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

def _reset_db():
    """Remove stale DB file and reset SQLAlchemy engine singleton."""
    db_file = "test_challenge.db"
    if os.path.exists(db_file):
        os.remove(db_file)
    import src.database.session as db_session
    db_session._engine = None
    db_session._SessionLocal = None

@pytest.fixture(autouse=True, scope="session")
def _clean_test_db():
    _reset_db()
    yield
    _reset_db()
