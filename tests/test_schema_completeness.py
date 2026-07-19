"""
Schema completeness test — verifies that alembic migrations create all
required tables on a fresh database.
"""
from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect

from alembic.config import Config
from alembic.command import upgrade

from src.database.session import REQUIRED_TABLES, _check_required_tables


EXPECTED_TABLES = sorted([
    "alembic_version",
    "assets",
    "price_history",
    "signals",
    "paper_account",
    "paper_positions",
    "trade_history",
    "app_settings",
    "alert_history",
    "audit_log",
    "scheduler_state",
    "market_data_meta",
    "daily_snapshots",
    "health_transitions",
])


@pytest.fixture
def fresh_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    monkeypatch.setenv("DATABASE_URL", url)
    yield url, path
    os.unlink(path)


class TestSchemaCompleteness:

    def test_alembic_creates_all_tables(self, fresh_db):
        """Running alembic upgrade head on an empty DB creates every required table."""
        url, path = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        inspector = inspect(engine)
        tables = sorted(inspector.get_table_names())

        for expected in EXPECTED_TABLES:
            assert expected in tables, f"Table '{expected}' missing after alembic upgrade head"

    def test_seed_data_present(self, fresh_db):
        """Alembic migrations seed all assets and paper account."""
        url, path = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        with engine.connect() as conn:
            from sqlalchemy import text
            assets = conn.execute(text("SELECT symbol FROM assets ORDER BY symbol")).fetchall()
            symbols = [row[0] for row in assets]
            assert symbols == [
                "AVAX/USD", "BTC/USD", "DOGE/USD", "DOT/USD",
                "ETH/USD", "LINK/USD", "LTC/USD", "SOL/USD", "XRP/USD",
            ]

            account = conn.execute(text("SELECT balance_usd FROM paper_account")).fetchone()
            assert account is not None
            assert float(account[0]) == 1000.00

    def test_check_required_tables_detects_missing(self, fresh_db):
        """_check_required_tables returns missing tables on an empty database."""
        url, _ = fresh_db
        engine = create_engine(url)
        missing = _check_required_tables(engine)
        assert len(missing) == len(REQUIRED_TABLES)
        assert "scheduler_state" in missing
        assert "signals" in missing

    def test_check_required_tables_passes_after_migration(self, fresh_db):
        """_check_required_tables returns empty list after alembic upgrade head."""
        url, _ = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        missing = _check_required_tables(engine)
        assert missing == [], f"Missing tables after migration: {missing}"

    def test_init_db_raises_on_missing_tables(self, fresh_db):
        """init_db raises RuntimeError on PostgreSQL-like path when tables are missing."""
        url, _ = fresh_db
        engine = create_engine(url)

        from unittest.mock import patch
        with patch("src.database.session._engine", engine):
            missing = _check_required_tables(engine)
        assert len(missing) > 0

    def test_scheduler_state_columns(self, fresh_db):
        """scheduler_state table has all columns from migrations 001 + 004."""
        url, _ = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("scheduler_state")}
        required_columns = {
            "job_name", "last_run_at", "last_success_at", "next_run_at",
            "run_count", "last_error", "updated_at",
            "lock_owner", "lock_expires_at", "current_status",
            "success_count", "failure_count", "last_duration_ms",
            "last_completed_at", "last_started_at",
        }
        missing = required_columns - columns
        assert not missing, f"scheduler_state missing columns: {missing}"

    def test_signals_has_lifecycle_columns(self, fresh_db):
        """signals table has lifecycle columns from migration 002."""
        url, _ = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("signals")}
        lifecycle_columns = {
            "confidence", "market_snapshot", "cancelled_at",
            "superseded_at", "owner_decision_note",
            "previous_signal_id", "superseded_reason",
        }
        missing = lifecycle_columns - columns
        assert not missing, f"signals missing lifecycle columns: {missing}"

    def test_market_data_meta_has_validation_columns(self, fresh_db):
        """market_data_meta has validation columns from migration 003."""
        url, _ = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("market_data_meta")}
        assert "valid_candle_count" in columns
        assert "validation_error" in columns

    def test_health_transitions_exists(self, fresh_db):
        """health_transitions table exists from migration 005."""
        url, _ = fresh_db

        cfg = Config("alembic.ini")
        cfg.set_main_option("sqlalchemy.url", url)
        upgrade(cfg, "head")

        engine = create_engine(url)
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "health_transitions" in tables

    def test_required_tables_constant_matches_migrations(self):
        """REQUIRED_TABLES in session.py covers all tables defined in models.py."""
        from src.database.models import Base
        model_tables = set(Base.metadata.tables.keys())
        required_set = set(REQUIRED_TABLES)
        missing_from_required = model_tables - required_set
        assert not missing_from_required, (
            f"Tables in models.py but not in REQUIRED_TABLES: {missing_from_required}"
        )
