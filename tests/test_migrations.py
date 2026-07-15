import os
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from src.database.models import Base


@pytest.fixture
def db_path():
    path = f"test_migration_{uuid.uuid4().hex[:8]}.db"
    yield path
    if os.path.exists(path):
        os.unlink(path)


class TestSchemaCreation:
    def test_all_tables_created(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        tables = inspector.get_table_names()

        expected_tables = [
            "assets", "price_history", "signals", "paper_account",
            "paper_positions", "trade_history", "app_settings",
            "alert_history", "audit_log", "scheduler_state",
            "market_data_meta", "daily_snapshots",
        ]
        for table in expected_tables:
            assert table in tables, f"Missing table: {table}"

        engine.dispose()

    def test_assets_table_columns(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("assets")}

        expected = {
            "id", "symbol", "kraken_pair", "coinbase_pair",
            "risk_pct", "max_position_usd", "stop_loss_pct",
            "min_volume", "enabled", "created_at", "updated_at",
        }
        assert expected.issubset(columns)
        engine.dispose()

    def test_signals_table_has_uuid_primary_key(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("signals")}

        assert "id" in columns
        assert str(columns["id"]["type"]) in ("VARCHAR(36)", "VARCHAR")
        engine.dispose()

    def test_paper_account_table(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("paper_account")}

        expected = {
            "id", "balance_usd", "peak_balance", "starting_balance",
            "realized_pnl", "daily_loss", "daily_loss_date",
            "challenge_status", "strategy_version", "updated_at",
        }
        assert expected.issubset(columns)
        engine.dispose()

    def test_unique_constraints(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        insert_sql = (
            "INSERT INTO assets (symbol, risk_pct, max_position_usd,"
            " stop_loss_pct, min_volume, enabled, created_at, updated_at)"
            " VALUES ('BTC/USD', 0.003, 150.0, 0.03, 0, 1,"
            " '2024-01-01T00:00:00', '2024-01-01T00:00:00')"
        )
        with engine.connect() as conn:
            conn.execute(text(insert_sql))
            conn.commit()
            with pytest.raises(Exception):
                conn.execute(text(insert_sql))
                conn.commit()

        engine.dispose()

    def test_drop_all_tables(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)
        assert len(inspector.get_table_names()) > 0

        Base.metadata.drop_all(engine)
        inspector = inspect(engine)
        assert len(inspector.get_table_names()) == 0

        engine.dispose()

    def test_foreign_keys(self, db_path):
        engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(engine)

        inspector = inspect(engine)

        signal_fks = inspector.get_foreign_keys("signals")
        fk_tables = [fk["referred_table"] for fk in signal_fks]
        assert "assets" in fk_tables

        position_fks = inspector.get_foreign_keys("paper_positions")
        fk_tables = [fk["referred_table"] for fk in position_fks]
        assert "assets" in fk_tables
        assert "signals" in fk_tables

        engine.dispose()
