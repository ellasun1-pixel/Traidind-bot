import os
import uuid
from datetime import datetime, timezone, timedelta, date

import pytest
from sqlalchemy import create_engine

from src.database.models import (
    Base, Asset, Signal, PaperAccount, PaperPosition,
    TradeHistory, AppSetting, AuditLog, SchedulerState,
    AlertHistory, DailySnapshot, MarketDataMeta,
)
from src.database.session import get_session, init_db, check_db_health, reset_engine
from src.database.repository import (
    AssetRepository, PaperAccountRepository, SignalRepository,
    PositionRepository, TradeHistoryRepository, AuditLogRepository,
    AlertHistoryRepository, SchedulerStateRepository,
    AppSettingRepository, DailySnapshotRepository,
)


@pytest.fixture
def db_engine():
    db_path = f"sqlite:///test_persistence_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(db_path, echo=False)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    db_file = db_path.replace("sqlite:///", "")
    if os.path.exists(db_file):
        os.unlink(db_file)


@pytest.fixture
def session(db_engine):
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    sess = factory()
    yield sess
    sess.close()


class TestDatabaseConnection:
    def test_health_check_ok(self, db_engine):
        result = check_db_health(db_engine)
        assert result["status"] == "ok"
        assert result["backend"] == "sqlite"

    def test_health_check_failure(self):
        bad_engine = create_engine("sqlite:///nonexistent/path/db.sqlite")
        result = check_db_health(bad_engine)
        assert result["status"] == "error"


class TestAssetRepository:
    def test_upsert_creates_new(self, session):
        repo = AssetRepository(session)
        asset = repo.upsert("TEST/USD", kraken_pair="TESTUSD", coinbase_pair="TEST-USD")
        session.commit()
        assert asset.id is not None
        assert asset.symbol == "TEST/USD"
        assert asset.kraken_pair == "TESTUSD"

    def test_upsert_updates_existing(self, session):
        repo = AssetRepository(session)
        asset1 = repo.upsert("TEST/USD", kraken_pair="TESTUSD")
        session.commit()
        asset2 = repo.upsert("TEST/USD", kraken_pair="NEWPAIR")
        session.commit()
        assert asset1.id == asset2.id
        assert asset2.kraken_pair == "NEWPAIR"

    def test_get_all_enabled(self, session):
        repo = AssetRepository(session)
        repo.upsert("A/USD", enabled=True)
        repo.upsert("B/USD", enabled=False)
        repo.upsert("C/USD", enabled=True)
        session.commit()
        enabled = repo.get_all_enabled()
        assert len(enabled) == 2
        symbols = [a.symbol for a in enabled]
        assert "A/USD" in symbols
        assert "C/USD" in symbols

    def test_get_by_symbol(self, session):
        repo = AssetRepository(session)
        repo.upsert("BTC/USD")
        session.commit()
        found = repo.get_by_symbol("BTC/USD")
        assert found is not None
        assert found.symbol == "BTC/USD"
        assert repo.get_by_symbol("NONEXISTENT") is None


class TestPaperAccountRepository:
    def test_get_or_create_new(self, session):
        repo = PaperAccountRepository(session)
        account = repo.get_or_create(starting_balance=1000.0)
        session.commit()
        assert account.id is not None
        assert float(account.balance_usd) == 1000.0
        assert float(account.peak_balance) == 1000.0

    def test_get_or_create_returns_existing(self, session):
        repo = PaperAccountRepository(session)
        a1 = repo.get_or_create(1000.0)
        session.commit()
        a2 = repo.get_or_create(2000.0)
        assert a1.id == a2.id
        assert float(a2.balance_usd) == 1000.0

    def test_update_balance(self, session):
        repo = PaperAccountRepository(session)
        account = repo.get_or_create(1000.0)
        repo.update_balance(account, 1050.0)
        session.commit()
        assert float(account.balance_usd) == 1050.0
        assert float(account.peak_balance) == 1050.0

    def test_peak_balance_only_increases(self, session):
        repo = PaperAccountRepository(session)
        account = repo.get_or_create(1000.0)
        repo.update_balance(account, 1050.0)
        repo.update_balance(account, 980.0)
        session.commit()
        assert float(account.balance_usd) == 980.0
        assert float(account.peak_balance) == 1050.0

    def test_reset_daily_loss(self, session):
        repo = PaperAccountRepository(session)
        account = repo.get_or_create(1000.0)
        account.daily_loss = 5.0
        account.daily_loss_date = date(2024, 1, 1)
        session.commit()
        repo.reset_daily_loss(account)
        session.commit()
        assert float(account.daily_loss) == 0.0


class TestSignalRepository:
    def _make_asset(self, session):
        repo = AssetRepository(session)
        asset = repo.upsert("BTC/USD")
        session.commit()
        return asset

    def test_create_signal(self, session):
        asset = self._make_asset(session)
        repo = SignalRepository(session)
        signal = repo.create(
            asset_id=asset.id,
            signal_type="BUY",
            priority="HIGH",
            regime="TREND",
            entry_price=50000.0,
            stop_loss=48500.0,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        session.commit()
        assert signal.id is not None
        assert len(signal.id) == 36
        assert signal.status == "pending"

    def test_get_pending(self, session):
        asset = self._make_asset(session)
        repo = SignalRepository(session)
        repo.create(
            asset_id=asset.id, signal_type="BUY", priority="HIGH",
            regime="TREND", expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        repo.create(
            asset_id=asset.id, signal_type="SELL", priority="MEDIUM",
            regime="CHOP", status="expired",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        session.commit()
        pending = repo.get_pending()
        assert len(pending) == 1
        assert pending[0].signal_type == "BUY"

    def test_expire_old_signals(self, session):
        asset = self._make_asset(session)
        repo = SignalRepository(session)
        repo.create(
            asset_id=asset.id, signal_type="BUY", priority="HIGH",
            regime="TREND",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        repo.create(
            asset_id=asset.id, signal_type="SELL", priority="MEDIUM",
            regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        session.commit()
        count = repo.expire_old_signals()
        session.commit()
        assert count == 1
        pending = repo.get_pending()
        assert len(pending) == 1

    def test_confirm_signal(self, session):
        asset = self._make_asset(session)
        repo = SignalRepository(session)
        signal = repo.create(
            asset_id=asset.id, signal_type="BUY", priority="HIGH",
            regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        session.commit()
        repo.confirm(signal)
        session.commit()
        assert signal.status == "confirmed"
        assert signal.confirmed_at is not None

    def test_reject_signal(self, session):
        asset = self._make_asset(session)
        repo = SignalRepository(session)
        signal = repo.create(
            asset_id=asset.id, signal_type="BUY", priority="HIGH",
            regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        session.commit()
        repo.reject(signal)
        session.commit()
        assert signal.status == "rejected"
        assert signal.rejected_at is not None


class TestPositionRepository:
    def test_create_and_close(self, session):
        asset_repo = AssetRepository(session)
        asset = asset_repo.upsert("BTC/USD")
        session.commit()

        pos_repo = PositionRepository(session)
        position = pos_repo.create(
            asset_id=asset.id, side="long", quantity=0.001,
            entry_price=50000.0, stop_loss=48500.0,
        )
        session.commit()
        assert position.is_open is True
        assert pos_repo.get_open_count() == 1

        pos_repo.close(position, exit_price=52000.0,
                       realized_pnl=2.0, close_reason="take_profit")
        session.commit()
        assert position.is_open is False
        assert float(position.realized_pnl) == 2.0
        assert pos_repo.get_open_count() == 0


class TestAuditLogRepository:
    def test_log_entry(self, session):
        repo = AuditLogRepository(session)
        entry = repo.log("test_action", "system", {"key": "value"})
        session.commit()
        assert entry.id is not None
        assert entry.action == "test_action"
        assert entry.detail == {"key": "value"}


class TestSchedulerStateRepository:
    def test_get_or_create(self, session):
        repo = SchedulerStateRepository(session)
        state = repo.get_or_create("market_check")
        session.commit()
        assert state.job_name == "market_check"
        assert state.run_count == 0

    def test_mark_started_and_success(self, session):
        repo = SchedulerStateRepository(session)
        repo.mark_started("market_check")
        session.commit()
        state = repo.get_or_create("market_check")
        assert state.run_count == 1
        assert state.last_run_at is not None

        repo.mark_success("market_check")
        session.commit()
        assert state.last_success_at is not None
        assert state.last_error is None

    def test_mark_failure(self, session):
        repo = SchedulerStateRepository(session)
        repo.mark_failure("market_check", "Connection timeout")
        session.commit()
        state = repo.get_or_create("market_check")
        assert state.last_error == "Connection timeout"


class TestAppSettingRepository:
    def test_set_and_get(self, session):
        repo = AppSettingRepository(session)
        repo.set("agent_mode", "PAUSED")
        session.commit()
        assert repo.get("agent_mode") == "PAUSED"

    def test_get_default(self, session):
        repo = AppSettingRepository(session)
        assert repo.get("nonexistent", "default_val") == "default_val"

    def test_update_existing(self, session):
        repo = AppSettingRepository(session)
        repo.set("mode", "A")
        session.commit()
        repo.set("mode", "B")
        session.commit()
        assert repo.get("mode") == "B"


class TestAlertHistoryRepository:
    def test_duplicate_detection(self, session):
        repo = AlertHistoryRepository(session)
        assert repo.is_duplicate("hash123") is False
        repo.record("signal", "hash123", asset_symbol="BTC/USD")
        session.commit()
        assert repo.is_duplicate("hash123") is True


class TestDailySnapshotRepository:
    def test_save_and_update(self, session):
        repo = DailySnapshotRepository(session)
        today = date.today()
        snap = repo.save_snapshot(
            snapshot_date=today, balance_usd=1015.0,
            realized_pnl=15.0, unrealized_pnl=5.0,
            open_positions_count=1, challenge_status="active",
            peak_balance=1020.0,
        )
        session.commit()
        assert snap.id is not None
        assert float(snap.balance_usd) == 1015.0

        snap2 = repo.save_snapshot(
            snapshot_date=today, balance_usd=1020.0,
            realized_pnl=20.0, unrealized_pnl=0.0,
            open_positions_count=0, challenge_status="active",
            peak_balance=1020.0,
        )
        session.commit()
        assert snap.id == snap2.id
        assert float(snap2.balance_usd) == 1020.0


class TestRestartRecovery:
    def test_data_survives_session_close_and_reopen(self, db_engine):
        from sqlalchemy.orm import sessionmaker
        factory = sessionmaker(bind=db_engine, expire_on_commit=False)

        sess1 = factory()
        asset_repo = AssetRepository(sess1)
        asset = asset_repo.upsert("BTC/USD", kraken_pair="XXBTZUSD")
        account_repo = PaperAccountRepository(sess1)
        account = account_repo.get_or_create(1000.0)
        account_repo.update_balance(account, 1042.50)
        signal_repo = SignalRepository(sess1)
        signal = signal_repo.create(
            asset_id=asset.id, signal_type="BUY", priority="HIGH",
            regime="TREND", entry_price=50000.0,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        signal_id = signal.id
        sess1.commit()
        sess1.close()

        sess2 = factory()
        asset_repo2 = AssetRepository(sess2)
        found_asset = asset_repo2.get_by_symbol("BTC/USD")
        assert found_asset is not None
        assert found_asset.kraken_pair == "XXBTZUSD"

        account_repo2 = PaperAccountRepository(sess2)
        account2 = account_repo2.get_or_create()
        assert float(account2.balance_usd) == 1042.50
        assert float(account2.peak_balance) == 1042.50

        signal_repo2 = SignalRepository(sess2)
        found_signal = signal_repo2.get_by_id(signal_id)
        assert found_signal is not None
        assert found_signal.signal_type == "BUY"
        assert found_signal.status == "pending"
        sess2.close()
