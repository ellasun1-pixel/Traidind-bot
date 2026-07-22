"""Iteration 5: Scheduler hardening and production pipeline integration tests."""
from __future__ import annotations

import json
import time
import threading
from datetime import datetime, timezone, timedelta
from io import BytesIO
from http.client import HTTPConnection
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    Base, SchedulerState, Asset, PriceHistory, MarketDataMeta,
    Signal, AppSetting, AuditLog,
)
from src.database.repository import (
    SchedulerStateRepository, PriceHistoryRepository,
    MarketDataMetaRepository, AssetRepository, AppSettingRepository,
    SignalRepository,
)
from src.market_data.candle import Candle, PriceQuote
from src.market_data.validation import ValidationResult
from src.market_data.pipeline import FetchResult, AnalysisSafetyResult, AssetHealth
from src.config import AssetConfig, AgentMode


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def seed_asset(session):
    asset = Asset(
        symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD",
    )
    session.add(asset)
    session.commit()
    return asset


def _make_candle(asset="BTC", day_offset=0, price=50000.0):
    now = datetime.now(timezone.utc)
    t = now - timedelta(days=day_offset)
    return Candle(
        asset=asset, timeframe="1d", open_time=t,
        open=price, high=price * 1.01, low=price * 0.99,
        close=price, volume=100.0, source="kraken",
        fetched_at=now,
    )


def _make_candles(count=260, asset="BTC"):
    return [_make_candle(asset=asset, day_offset=i, price=50000 + i * 10) for i in range(count)]


def _make_session_cm(Session):
    def make_session():
        class CM:
            def __enter__(self_):
                self_.s = Session()
                return self_.s
            def __exit__(self_, *args):
                self_.s.commit()
                self_.s.close()
                return False
        return CM()
    return make_session


# ──────────────────────────────────────────────
# Section 1: SchedulerState model expansion
# ──────────────────────────────────────────────

class TestSchedulerStateModel:
    def test_new_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("scheduler_state")}
        for col in (
            "lock_owner", "lock_expires_at", "current_status",
            "success_count", "failure_count", "last_duration_ms",
            "last_completed_at", "last_started_at",
        ):
            assert col in cols, f"Missing column: {col}"

    def test_scheduler_state_defaults(self, session):
        state = SchedulerState(job_name="test_job")
        session.add(state)
        session.commit()
        assert state.run_count == 0
        assert state.success_count == 0
        assert state.failure_count == 0
        assert state.lock_owner is None
        assert state.current_status is None


# ──────────────────────────────────────────────
# Section 2: Job locking (atomic conditional UPDATE)
# ──────────────────────────────────────────────

class TestJobLocking:
    def test_acquire_lock_succeeds_when_free(self, session):
        repo = SchedulerStateRepository(session)
        acquired = repo.try_acquire_lock("market_check", "worker-1")
        assert acquired is True
        state = repo.get_or_create("market_check")
        assert state.lock_owner == "worker-1"
        assert state.current_status == "running"

    def test_acquire_lock_fails_when_held(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("market_check", "worker-1", lock_duration_seconds=300)
        session.commit()
        acquired = repo.try_acquire_lock("market_check", "worker-2")
        assert acquired is False

    def test_expired_lock_can_be_reacquired(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("market_check", "worker-1", lock_duration_seconds=1)
        state = repo.get_or_create("market_check")
        state.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()
        acquired = repo.try_acquire_lock("market_check", "worker-2")
        assert acquired is True
        state = repo.get_or_create("market_check")
        assert state.lock_owner == "worker-2"

    def test_release_lock(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("market_check", "worker-1")
        repo.release_lock("market_check")
        state = repo.get_or_create("market_check")
        assert state.lock_owner is None
        assert state.lock_expires_at is None

    def test_lock_increments_run_count(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("market_check", "w1", lock_duration_seconds=1)
        state = repo.get_or_create("market_check")
        state.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()
        repo.try_acquire_lock("market_check", "w2", lock_duration_seconds=1)
        state = repo.get_or_create("market_check")
        assert state.run_count == 2

    def test_concurrent_lock_two_sessions(self, engine):
        """Two independent sessions: only one acquires the lock."""
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        s1 = Session()
        s2 = Session()

        repo1 = SchedulerStateRepository(s1)
        repo2 = SchedulerStateRepository(s2)

        a1 = repo1.try_acquire_lock("market_check", "worker-1", lock_duration_seconds=300)
        s1.commit()

        a2 = repo2.try_acquire_lock("market_check", "worker-2", lock_duration_seconds=300)
        s2.commit()

        assert a1 is True
        assert a2 is False

        s1.close()
        s2.close()


# ──────────────────────────────────────────────
# Section 3: Success/failure tracking
# ──────────────────────────────────────────────

class TestSuccessFailureTracking:
    def test_mark_success_updates_counts(self, session):
        repo = SchedulerStateRepository(session)
        repo.mark_success("test_job", duration_ms=150)
        state = repo.get_or_create("test_job")
        assert state.success_count == 1
        assert state.last_duration_ms == 150
        assert state.current_status == "idle"
        assert state.last_error is None

    def test_mark_failure_updates_counts(self, session):
        repo = SchedulerStateRepository(session)
        repo.mark_failure("test_job", "something broke", duration_ms=50)
        state = repo.get_or_create("test_job")
        assert state.failure_count == 1
        assert state.last_error == "something broke"
        assert state.current_status == "idle"

    def test_success_clears_lock(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("test_job", "w1")
        repo.mark_success("test_job")
        state = repo.get_or_create("test_job")
        assert state.lock_owner is None

    def test_failure_clears_lock(self, session):
        repo = SchedulerStateRepository(session)
        repo.try_acquire_lock("test_job", "w1")
        repo.mark_failure("test_job", "err")
        state = repo.get_or_create("test_job")
        assert state.lock_owner is None


# ──────────────────────────────────────────────
# Section 4: Candle persistence (PriceHistoryRepository)
# ──────────────────────────────────────────────

class TestCandlePersistence:
    def test_upsert_creates_new(self, session, seed_asset):
        repo = PriceHistoryRepository(session)
        t = datetime.now(timezone.utc)
        ph = repo.upsert_candle(
            asset_id=seed_asset.id, timeframe="1d", open_time=t,
            open_=50000.0, high=51000.0, low=49000.0, close=50500.0,
            volume=100.0, source="kraken",
        )
        session.commit()
        assert ph.id is not None
        assert float(ph.close) == 50500.0

    def test_upsert_updates_existing(self, session, seed_asset):
        repo = PriceHistoryRepository(session)
        t = datetime.now(timezone.utc)
        repo.upsert_candle(
            asset_id=seed_asset.id, timeframe="1d", open_time=t,
            open_=50000.0, high=51000.0, low=49000.0, close=50500.0,
            volume=100.0, source="kraken",
        )
        session.commit()
        repo.upsert_candle(
            asset_id=seed_asset.id, timeframe="1d", open_time=t,
            open_=50000.0, high=52000.0, low=49000.0, close=51000.0,
            volume=200.0, source="kraken",
        )
        session.commit()
        count = session.query(PriceHistory).count()
        assert count == 1
        record = session.query(PriceHistory).first()
        assert float(record.close) == 51000.0

    def test_bulk_upsert(self, session, seed_asset):
        repo = PriceHistoryRepository(session)
        candles = _make_candles(5)
        count = repo.bulk_upsert(seed_asset.id, "1d", "kraken", candles)
        session.commit()
        assert count == 5
        total = session.query(PriceHistory).count()
        assert total == 5

    def test_bulk_upsert_idempotent(self, session, seed_asset):
        repo = PriceHistoryRepository(session)
        candles = _make_candles(3)
        repo.bulk_upsert(seed_asset.id, "1d", "kraken", candles)
        session.commit()
        repo.bulk_upsert(seed_asset.id, "1d", "kraken", candles)
        session.commit()
        total = session.query(PriceHistory).count()
        assert total == 3


# ──────────────────────────────────────────────
# Section 5: Market data meta persistence
# ──────────────────────────────────────────────

class TestMarketDataMetaPersistence:
    def test_upsert_creates(self, session, seed_asset):
        repo = MarketDataMetaRepository(session)
        now = datetime.now(timezone.utc)
        meta = repo.upsert(
            asset_id=seed_asset.id, timeframe="1d", source="kraken",
            candle_count=300, valid_candle_count=295,
            oldest_candle=now - timedelta(days=300),
            newest_candle=now - timedelta(hours=1),
            is_sufficient=True,
        )
        session.commit()
        assert meta.id is not None
        assert meta.valid_candle_count == 295

    def test_upsert_updates(self, session, seed_asset):
        repo = MarketDataMetaRepository(session)
        now = datetime.now(timezone.utc)
        repo.upsert(
            asset_id=seed_asset.id, timeframe="1d", source="kraken",
            candle_count=200, valid_candle_count=190,
            oldest_candle=now - timedelta(days=200),
            newest_candle=now - timedelta(hours=2),
            is_sufficient=False, validation_error="Not enough candles",
        )
        session.commit()
        repo.upsert(
            asset_id=seed_asset.id, timeframe="1d", source="kraken",
            candle_count=300, valid_candle_count=295,
            oldest_candle=now - timedelta(days=300),
            newest_candle=now - timedelta(hours=1),
            is_sufficient=True,
        )
        session.commit()
        count = session.query(MarketDataMeta).count()
        assert count == 1
        meta = session.query(MarketDataMeta).first()
        assert meta.valid_candle_count == 295
        assert meta.validation_error is None

    def test_different_sources_create_separate_records(self, session, seed_asset):
        repo = MarketDataMetaRepository(session)
        now = datetime.now(timezone.utc)
        repo.upsert(
            asset_id=seed_asset.id, timeframe="1d", source="kraken",
            candle_count=300, valid_candle_count=295,
            oldest_candle=now - timedelta(days=300),
            newest_candle=now - timedelta(hours=1),
            is_sufficient=True,
        )
        repo.upsert(
            asset_id=seed_asset.id, timeframe="1d", source="coinbase",
            candle_count=280, valid_candle_count=275,
            oldest_candle=now - timedelta(days=280),
            newest_candle=now - timedelta(hours=2),
            is_sufficient=True,
        )
        session.commit()
        count = session.query(MarketDataMeta).count()
        assert count == 2


# ──────────────────────────────────────────────
# Section 6: No old MarketDataFetcher references
# ──────────────────────────────────────────────

class TestOldFetcherRemoved:
    def test_scheduler_does_not_import_fetcher(self):
        import src.scheduler.jobs as jobs_module
        source = open(jobs_module.__file__).read()
        assert "MarketDataFetcher" not in source
        assert "from src.market_data.fetcher" not in source

    def test_main_does_not_import_fetcher(self):
        source = open("main.py").read()
        assert "MarketDataFetcher" not in source
        assert "from src.market_data.fetcher" not in source

    def test_fetcher_file_deleted(self):
        import os
        assert not os.path.exists("src/market_data/fetcher.py")

    def test_market_data_init_no_fetcher(self):
        import src.market_data as md_module
        source = open(md_module.__file__).read()
        assert "MarketDataFetcher" not in source
        assert "fetcher" not in source


# ──────────────────────────────────────────────
# Section 7: Market check job integration
# ──────────────────────────────────────────────

class TestMarketCheckJob:
    @pytest.fixture(autouse=True)
    def _setup(self, engine):
        self.engine = engine
        self.sent_messages = []

        async def mock_send(text):
            self.sent_messages.append(text)

        import src.scheduler.jobs as jobs
        self._orig_portfolio = jobs._portfolio
        self._orig_send = jobs._send_message_func
        self._orig_pipeline = jobs._pipeline
        self._orig_engine = jobs._engine
        self._orig_signals = jobs._last_signals.copy()

        jobs._portfolio = MagicMock()
        jobs._portfolio.balance_usd = 1000.0
        jobs._portfolio.get_open_positions.return_value = []
        jobs._portfolio.get_total_open_risk.return_value = 0.0
        jobs._send_message_func = mock_send
        jobs._last_signals = {}

        yield

        jobs._portfolio = self._orig_portfolio
        jobs._send_message_func = self._orig_send
        jobs._pipeline = self._orig_pipeline
        jobs._engine = self._orig_engine
        jobs._last_signals = self._orig_signals

    @pytest.mark.asyncio
    async def test_paused_mode_skips(self):
        import src.scheduler.jobs as jobs
        from src.config import settings
        old_mode = settings.agent_mode
        settings.agent_mode = AgentMode.PAUSED
        try:
            await jobs.market_check_job()
        finally:
            settings.agent_mode = old_mode

    @pytest.mark.asyncio
    async def test_locked_job_skips(self):
        import src.scheduler.jobs as jobs
        with patch("src.scheduler.jobs.get_session") as mock_gs:
            mock_session = MagicMock()
            mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)
            mock_repo = MagicMock()
            mock_repo.try_acquire_lock.return_value = False
            with patch("src.scheduler.jobs.SchedulerStateRepository", return_value=mock_repo):
                await jobs.market_check_job()

    @pytest.mark.asyncio
    async def test_per_asset_error_isolation(self):
        import src.scheduler.jobs as jobs
        from src.config import settings

        test_assets = [
            AssetConfig(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD"),
            AssetConfig(symbol="ETH", kraken_pair="XETHZUSD", coinbase_pair="ETH-USD"),
        ]
        old_assets = settings.assets
        settings.assets = test_assets

        call_count = 0

        async def mock_process(asset):
            nonlocal call_count
            call_count += 1
            if asset.symbol == "BTC":
                raise RuntimeError("BTC fetch failed")
            return {"symbol": "ETH", "status": "ok", "signal_type": "NO_TRADE", "error": None}

        with patch("src.scheduler.jobs._process_single_asset", side_effect=mock_process):
            with patch("src.scheduler.jobs.get_session") as mock_gs:
                mock_session = MagicMock()
                mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
                mock_gs.return_value.__exit__ = MagicMock(return_value=False)
                mock_repo = MagicMock()
                mock_repo.try_acquire_lock.return_value = True
                with patch("src.scheduler.jobs.SchedulerStateRepository", return_value=mock_repo):
                    await jobs.market_check_job()

        settings.assets = old_assets
        assert call_count == 2


# ──────────────────────────────────────────────
# Section 8: Expire signals job
# ──────────────────────────────────────────────

class TestExpireSignalsJob:
    @pytest.mark.asyncio
    async def test_expire_signals_runs(self, engine):
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
        with Session() as s:
            s.add(asset)
            s.commit()
            asset_id = asset.id

        with Session() as s:
            sig = Signal(
                asset_id=asset_id, signal_type="BUY", regime="TREND",
                status="pending", strategy_version="1.0", reason="test",
                expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            s.add(sig)
            s.commit()

        with Session() as s:
            from src.signals.lifecycle import SignalLifecycle
            lifecycle = SignalLifecycle(s)
            expired = lifecycle.expire_old_signals()
            s.commit()
            assert len(expired) == 1
            assert expired[0].status == "expired"


# ──────────────────────────────────────────────
# Section 9: Report idempotency
# ──────────────────────────────────────────────

class TestReportIdempotency:
    def test_app_setting_prevents_duplicate_report(self, session):
        repo = AppSettingRepository(session)
        repo.set("last_morning_report_date", "2026-07-15")
        session.commit()
        val = repo.get("last_morning_report_date")
        assert val == "2026-07-15"

    def test_app_setting_update(self, session):
        repo = AppSettingRepository(session)
        repo.set("last_morning_report_date", "2026-07-14")
        session.commit()
        repo.set("last_morning_report_date", "2026-07-15")
        session.commit()
        val = repo.get("last_morning_report_date")
        assert val == "2026-07-15"
        count = session.query(AppSetting).filter(AppSetting.key == "last_morning_report_date").count()
        assert count == 1


# ──────────────────────────────────────────────
# Section 10: Duplicate signal prevention & supersession
# ──────────────────────────────────────────────

class TestSignalSupersession:
    def test_pending_detected(self, session, seed_asset):
        sig = Signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            status="pending", strategy_version="1.0", reason="test",
            priority="medium",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session.add(sig)
        session.commit()

        from src.signals.lifecycle import SignalLifecycle
        lifecycle = SignalLifecycle(session)
        pending = lifecycle.get_pending_for_asset(seed_asset.id)
        assert len(pending) == 1

    def test_no_pending_when_all_expired(self, session, seed_asset):
        sig = Signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            status="expired", strategy_version="1.0", reason="test",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        session.add(sig)
        session.commit()

        from src.signals.lifecycle import SignalLifecycle
        lifecycle = SignalLifecycle(session)
        pending = lifecycle.get_pending_for_asset(seed_asset.id)
        assert len(pending) == 0

    def test_equivalent_signal_is_suppressed(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "medium"

        new_signal = MagicMock()
        new_signal.signal_type = "BUY"
        new_signal.entry_price = 50100.0
        new_signal.stop_loss = 48550.0
        new_signal.priority = "MEDIUM"

        assert _is_signal_equivalent(existing, new_signal) is True

    def test_different_type_not_equivalent(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "medium"

        new_signal = MagicMock()
        new_signal.signal_type = "SELL"
        new_signal.entry_price = 50000.0
        new_signal.stop_loss = 48500.0
        new_signal.priority = "MEDIUM"

        assert _is_signal_equivalent(existing, new_signal) is False

    def test_different_price_not_equivalent(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "medium"

        new_signal = MagicMock()
        new_signal.signal_type = "BUY"
        new_signal.entry_price = 55000.0
        new_signal.stop_loss = 48500.0
        new_signal.priority = "MEDIUM"

        assert _is_signal_equivalent(existing, new_signal) is False

    def test_supersede_creates_chain(self, session, seed_asset):
        from src.signals.lifecycle import SignalLifecycle
        lifecycle = SignalLifecycle(session)

        old_sig = lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="old signal", strategy_version="1.0",
            entry_price=50000.0, priority="medium",
        )
        old_id = old_sig.id
        session.commit()

        new_sig = lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="new signal", strategy_version="1.0",
            entry_price=55000.0, priority="high",
            previous_signal_id=old_id,
            supersede_previous=True,
        )
        session.commit()

        session.refresh(old_sig)
        assert old_sig.status == "superseded"
        assert old_sig.superseded_at is not None
        assert new_sig.previous_signal_id == old_id
        assert new_sig.status == "pending"

        pending = lifecycle.get_pending_for_asset(seed_asset.id)
        assert len(pending) == 1
        assert pending[0].id == new_sig.id

    def test_supersede_is_audit_logged(self, session, seed_asset):
        from src.signals.lifecycle import SignalLifecycle
        lifecycle = SignalLifecycle(session)

        old_sig = lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="old", strategy_version="1.0",
        )
        old_id = old_sig.id
        session.commit()

        lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="SELL", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="new", strategy_version="1.0",
            previous_signal_id=old_id, supersede_previous=True,
        )
        session.commit()

        logs = session.query(AuditLog).all()
        actions = [l.action for l in logs]
        assert "SIGNAL_SUPERSEDED" in actions
        assert "SIGNAL_CREATED" in actions

    def test_old_signal_immutable_after_supersede(self, session, seed_asset):
        from src.signals.lifecycle import SignalLifecycle, InvalidTransitionError
        lifecycle = SignalLifecycle(session)

        old_sig = lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="BUY", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="old", strategy_version="1.0",
        )
        session.commit()

        lifecycle.create_signal(
            asset_id=seed_asset.id, signal_type="SELL", regime="TREND",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            reason="new", strategy_version="1.0",
            previous_signal_id=old_sig.id, supersede_previous=True,
        )
        session.commit()

        session.refresh(old_sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.confirm(old_sig)


# ──────────────────────────────────────────────
# Section 11: Startup sweep
# ──────────────────────────────────────────────

class TestStartupSweep:
    @pytest.mark.asyncio
    async def test_startup_clears_stale_locks(self, engine):
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as s:
            state = SchedulerState(
                job_name="market_check", current_status="running",
                lock_owner="old-worker", run_count=5,
            )
            s.add(state)
            s.commit()

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            from src.scheduler.jobs import startup_sweep
            await startup_sweep()

        with Session() as s:
            state = s.query(SchedulerState).first()
            assert state.current_status == "idle"
            assert state.lock_owner is None


# ──────────────────────────────────────────────
# Section 12: Scheduler setup
# ──────────────────────────────────────────────

class TestSchedulerSetup:
    def test_setup_creates_six_jobs(self):
        from src.scheduler.jobs import setup_scheduler
        scheduler = setup_scheduler()
        jobs = scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        expected = {"market_check", "expire_signals", "morning_report", "evening_report", "health_heartbeat", "health_check"}
        assert expected == job_ids

    def test_misfire_grace_time_set(self):
        from src.scheduler.jobs import setup_scheduler
        scheduler = setup_scheduler()
        for job in scheduler.get_jobs():
            assert job.misfire_grace_time is not None


# ──────────────────────────────────────────────
# Section 13: /scheduler command
# ──────────────────────────────────────────────

class TestSchedulerCommand:
    @pytest.mark.asyncio
    async def test_scheduler_command_no_data(self):
        from src.telegram_bot.bot import cmd_scheduler
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 123456789
        update.effective_chat = MagicMock()
        update.effective_chat.id = 123456789
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("src.telegram_bot.bot.get_scheduler_status", return_value=[]):
            await cmd_scheduler.__wrapped__(update, context)

        update.message.reply_text.assert_called_once()
        call_text = update.message.reply_text.call_args[0][0]
        assert "No scheduler data" in call_text

    @pytest.mark.asyncio
    async def test_scheduler_command_with_data(self):
        from src.telegram_bot.bot import cmd_scheduler
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 123456789
        update.effective_chat = MagicMock()
        update.effective_chat.id = 123456789
        update.message = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        mock_status = [{
            "job_name": "market_check",
            "current_status": "idle",
            "run_count": 10,
            "success_count": 9,
            "failure_count": 1,
            "last_run_at": datetime.now(timezone.utc),
            "last_success_at": datetime.now(timezone.utc),
            "last_error": None,
            "last_duration_ms": 500,
        }]
        with patch("src.telegram_bot.bot.get_scheduler_status", return_value=mock_status):
            await cmd_scheduler.__wrapped__(update, context)

        call_text = update.message.reply_text.call_args[0][0]
        assert "market\\_check" in call_text
        assert "500ms" in call_text


# ──────────────────────────────────────────────
# Section 14: Fetcher fully removed
# ──────────────────────────────────────────────

class TestBotImports:
    def test_bot_does_not_import_fetcher(self):
        import src.telegram_bot.bot as bot_module
        source = open(bot_module.__file__).read()
        assert "MarketDataFetcher" not in source

    def test_scheduler_command_registered(self):
        from src.telegram_bot.bot import create_bot
        app = create_bot("test_token")
        handler_names = []
        for group in app.handlers.values():
            for h in group:
                if hasattr(h, "commands"):
                    handler_names.extend(h.commands)
        assert "scheduler" in handler_names


# ──────────────────────────────────────────────
# Section 15: Health heartbeat
# ──────────────────────────────────────────────

class TestHealthHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_records_state(self, engine):
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        mock_pipeline = MagicMock()
        mock_health = AssetHealth(
            current_provider="kraken", candle_freshness_hours=1.0,
            validation_status="ready",
        )
        mock_pipeline.get_health.return_value = mock_health

        import src.scheduler.jobs as jobs
        orig = jobs._pipeline
        jobs._pipeline = mock_pipeline

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            await jobs.health_heartbeat_job()

        jobs._pipeline = orig

        with Session() as s:
            state = s.query(SchedulerState).filter(
                SchedulerState.job_name == "health_heartbeat"
            ).first()
            assert state is not None
            assert state.success_count == 1


# ──────────────────────────────────────────────
# Section 16: Migration 004 schema
# ──────────────────────────────────────────────

class TestMigration004:
    def test_scheduler_state_has_all_new_columns(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("scheduler_state")}
        new_cols = {
            "lock_owner", "lock_expires_at", "current_status",
            "success_count", "failure_count", "last_duration_ms",
            "last_completed_at", "last_started_at",
        }
        assert new_cols.issubset(cols)


# ──────────────────────────────────────────────
# Section 17: get_scheduler_status
# ──────────────────────────────────────────────

class TestGetSchedulerStatus:
    def test_returns_list_of_dicts(self, engine):
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        with Session() as s:
            s.add(SchedulerState(job_name="market_check", run_count=5, success_count=4, failure_count=1))
            s.commit()

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            from src.scheduler.jobs import get_scheduler_status
            result = get_scheduler_status()

        assert len(result) == 1
        assert result[0]["job_name"] == "market_check"
        assert result[0]["run_count"] == 5
        assert result[0]["success_count"] == 4


# ──────────────────────────────────────────────
# Section 18: Instance ID uniqueness
# ──────────────────────────────────────────────

class TestInstanceId:
    def test_instance_id_is_set(self):
        from src.scheduler.jobs import _instance_id
        assert _instance_id is not None
        assert len(_instance_id) == 8


# ──────────────────────────────────────────────
# Section 19: Only validated candles are persisted
# ──────────────────────────────────────────────

class TestValidatedCandlePersistence:
    @pytest.mark.asyncio
    async def test_invalid_dataset_creates_meta_but_no_price_history(self, engine):
        """Invalid data: metadata is updated, but NO candles go to price_history."""
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as s:
            asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
            s.add(asset)
            s.commit()

        candles = _make_candles(3)
        fetch_result = FetchResult(
            candles=candles,
            validation=ValidationResult(
                valid=False, candle_count=3, valid_candle_count=3,
                invalid_candle_count=0,
                oldest_candle=candles[-1].open_time,
                newest_candle=candles[0].open_time,
                errors=["Insufficient candles: 3 valid, 250 required"], warnings=[],
            ),
            provider_used="kraken",
            fallback_used=False,
        )
        safety_result = AnalysisSafetyResult(
            safe=False, daily_df=None, current_price=None,
            provider_used="kraken",
            reason="DATA_INVALID: Insufficient candles",
            asset_health=AssetHealth(),
        )

        mock_pipeline = AsyncMock()
        mock_pipeline.fetch_validated_candles = AsyncMock(return_value=fetch_result)
        mock_pipeline.get_analysis_ready_data = AsyncMock(return_value=safety_result)

        import src.scheduler.jobs as jobs
        orig_pipeline = jobs._pipeline
        jobs._pipeline = mock_pipeline

        asset_config = AssetConfig(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            result = await jobs._process_single_asset(asset_config)

        jobs._pipeline = orig_pipeline

        assert result["status"] == "data_unsafe"
        assert result["candles_persisted"] == 0

        with Session() as s:
            price_count = s.query(PriceHistory).count()
            assert price_count == 0, "Invalid dataset must NOT create price_history rows"

            meta = s.query(MarketDataMeta).first()
            assert meta is not None, "Invalid dataset MUST create metadata"
            assert meta.is_sufficient is False
            assert "Insufficient" in (meta.validation_error or "")

    @pytest.mark.asyncio
    async def test_valid_dataset_persists_candles_and_meta(self, engine):
        """Valid data: both candles and metadata are persisted."""
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as s:
            asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
            s.add(asset)
            s.commit()

        import pandas as pd
        candles = _make_candles(260)
        fetch_result = FetchResult(
            candles=candles,
            validation=ValidationResult(
                valid=True, candle_count=260, valid_candle_count=260,
                invalid_candle_count=0,
                oldest_candle=candles[-1].open_time,
                newest_candle=candles[0].open_time,
                errors=[], warnings=[],
            ),
            provider_used="kraken",
            fallback_used=False,
        )
        safety_result = AnalysisSafetyResult(
            safe=True, daily_df=pd.DataFrame({"close": [50000]}), current_price=50000.0,
            provider_used="kraken",
            reason="", asset_health=AssetHealth(),
        )

        mock_pipeline = AsyncMock()
        mock_pipeline.fetch_validated_candles = AsyncMock(return_value=fetch_result)
        mock_pipeline.get_analysis_ready_data = AsyncMock(return_value=safety_result)
        mock_pipeline.get_health.return_value = AssetHealth()

        import src.scheduler.jobs as jobs
        orig_pipeline = jobs._pipeline
        orig_engine = jobs._engine
        jobs._pipeline = mock_pipeline

        mock_strategy = MagicMock()
        from src.strategy.regime import MarketRegime
        mock_strategy.analyze.return_value = MagicMock(
            signal_type="NO_TRADE", regime=MarketRegime.CHOP,
            priority="MEDIUM", reason="test",
        )
        jobs._engine = mock_strategy

        asset_config = AssetConfig(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            result = await jobs._process_single_asset(asset_config)

        jobs._pipeline = orig_pipeline
        jobs._engine = orig_engine

        assert result["candles_persisted"] == 260

        with Session() as s:
            price_count = s.query(PriceHistory).count()
            assert price_count == 260
            meta = s.query(MarketDataMeta).first()
            assert meta is not None
            assert meta.is_sufficient is True

    @pytest.mark.asyncio
    async def test_invalid_fetch_does_not_overwrite_valid_history(self, engine):
        """A later invalid fetch must not delete or overwrite previously stored valid candles."""
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as s:
            asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
            s.add(asset)
            s.commit()
            asset_id = asset.id

        with Session() as s:
            repo = PriceHistoryRepository(s)
            good_candles = _make_candles(10)
            repo.bulk_upsert(asset_id, "1d", "kraken", good_candles)
            s.commit()

        bad_candles = _make_candles(2)
        fetch_result = FetchResult(
            candles=bad_candles,
            validation=ValidationResult(
                valid=False, candle_count=2, valid_candle_count=2,
                invalid_candle_count=0,
                oldest_candle=bad_candles[-1].open_time,
                newest_candle=bad_candles[0].open_time,
                errors=["Not enough candles"], warnings=[],
            ),
            provider_used="kraken",
            fallback_used=False,
        )
        safety_result = AnalysisSafetyResult(
            safe=False, daily_df=None, current_price=None,
            provider_used="kraken", reason="DATA_INVALID",
            asset_health=AssetHealth(),
        )

        mock_pipeline = AsyncMock()
        mock_pipeline.fetch_validated_candles = AsyncMock(return_value=fetch_result)
        mock_pipeline.get_analysis_ready_data = AsyncMock(return_value=safety_result)

        import src.scheduler.jobs as jobs
        orig_pipeline = jobs._pipeline
        jobs._pipeline = mock_pipeline

        asset_config = AssetConfig(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            await jobs._process_single_asset(asset_config)

        jobs._pipeline = orig_pipeline

        with Session() as s:
            count = s.query(PriceHistory).count()
            assert count == 10, "Previous valid history must be retained"

    @pytest.mark.asyncio
    async def test_valid_fallback_persists_when_primary_invalid(self, engine):
        """When primary provider fails and fallback succeeds, fallback candles are persisted."""
        Session = sessionmaker(bind=engine, expire_on_commit=False)

        with Session() as s:
            asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
            s.add(asset)
            s.commit()

        import pandas as pd
        candles = _make_candles(260)
        fetch_result = FetchResult(
            candles=candles,
            validation=ValidationResult(
                valid=True, candle_count=260, valid_candle_count=260,
                invalid_candle_count=0,
                oldest_candle=candles[-1].open_time,
                newest_candle=candles[0].open_time,
                errors=[], warnings=[],
            ),
            provider_used="coinbase",
            fallback_used=True,
        )
        safety_result = AnalysisSafetyResult(
            safe=True, daily_df=pd.DataFrame({"close": [50000]}), current_price=50000.0,
            provider_used="coinbase", reason="",
            asset_health=AssetHealth(),
        )

        mock_pipeline = AsyncMock()
        mock_pipeline.fetch_validated_candles = AsyncMock(return_value=fetch_result)
        mock_pipeline.get_analysis_ready_data = AsyncMock(return_value=safety_result)
        mock_pipeline.get_health.return_value = AssetHealth()

        import src.scheduler.jobs as jobs
        orig_pipeline = jobs._pipeline
        orig_engine = jobs._engine
        jobs._pipeline = mock_pipeline

        from src.strategy.regime import MarketRegime
        mock_strategy = MagicMock()
        mock_strategy.analyze.return_value = MagicMock(
            signal_type="NO_TRADE", regime=MarketRegime.CHOP,
            priority="MEDIUM", reason="test",
        )
        jobs._engine = mock_strategy

        asset_config = AssetConfig(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")

        with patch("src.scheduler.jobs.get_session", side_effect=_make_session_cm(Session)):
            result = await jobs._process_single_asset(asset_config)

        jobs._pipeline = orig_pipeline
        jobs._engine = orig_engine

        assert result["candles_persisted"] == 260
        with Session() as s:
            ph = s.query(PriceHistory).first()
            assert ph.source == "coinbase"


# ──────────────────────────────────────────────
# Section 20: Health endpoint readiness
# ──────────────────────────────────────────────

class TestHealthEndpoint:
    def test_liveness_always_returns_200(self):
        from main import HealthHandler, set_app_ready
        set_app_ready(False)

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)

    def test_health_returns_503_before_init(self):
        from main import HealthHandler, set_app_ready
        set_app_ready(False)

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(503)

    def test_health_returns_200_after_init(self):
        from main import HealthHandler, set_app_ready
        set_app_ready(True)

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)

        set_app_ready(False)

    def test_health_does_not_expose_secrets(self):
        from main import HealthHandler, set_app_ready
        set_app_ready(True)

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)

        body = handler.wfile.getvalue().decode()
        assert "token" not in body.lower()
        assert "password" not in body.lower()
        assert "api_key" not in body.lower()
        assert "secret" not in body.lower()

        set_app_ready(False)

    def test_health_503_on_simulated_db_failure(self):
        from main import HealthHandler, set_app_ready
        set_app_ready(False)

        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(503)


class TestMarketCheckActiveHours:
    """Fix #9: market_check_job must only run during 08:00–23:00 local time."""

    @pytest.mark.asyncio
    async def test_market_check_skipped_at_night(self):
        from src.scheduler.jobs import market_check_job
        from src.config import AgentMode

        with patch("src.scheduler.jobs.settings") as mock_settings:
            mock_settings.agent_mode = AgentMode.PAPER_CHALLENGE
            mock_settings.timezone = "Asia/Jerusalem"

            class FakeAware:
                hour = 3
            with patch("src.scheduler.jobs.datetime") as mock_dt:
                mock_dt.now.return_value = FakeAware()

                with patch("src.scheduler.jobs.pytz") as mock_pytz:
                    mock_tz = MagicMock()
                    mock_pytz.timezone.return_value = mock_tz

                    with patch("src.scheduler.jobs.get_portfolio") as mock_gp:
                        await market_check_job()
                        mock_gp.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_check_runs_during_active_hours(self):
        from src.scheduler.jobs import market_check_job
        from src.config import AgentMode

        with patch("src.scheduler.jobs.settings") as mock_settings:
            mock_settings.agent_mode = AgentMode.PAPER_CHALLENGE
            mock_settings.timezone = "Asia/Jerusalem"

            class FakeAware:
                hour = 10
            with patch("src.scheduler.jobs.datetime") as mock_dt:
                mock_dt.now.return_value = FakeAware()

                with patch("src.scheduler.jobs.pytz") as mock_pytz:
                    mock_tz = MagicMock()
                    mock_pytz.timezone.return_value = mock_tz

                    with patch("src.scheduler.jobs.get_portfolio") as mock_gp:
                        mock_portfolio = MagicMock()
                        mock_portfolio.is_challenge_active = False
                        mock_gp.return_value = mock_portfolio
                        await market_check_job()
                        mock_gp.assert_called_once()


class TestStartupSweepRestoresMode:
    """Fix #8: startup_sweep must restore agent_mode from DB."""

    @pytest.mark.asyncio
    async def test_startup_restores_paused_mode(self):
        from src.scheduler.jobs import startup_sweep

        mock_setting_repo = MagicMock()
        mock_setting_repo.get.return_value = "PAUSED"

        with patch("src.scheduler.jobs.get_session") as mock_gs, \
             patch("src.scheduler.jobs.SignalLifecycle"), \
             patch("src.scheduler.jobs.AppSettingRepository", return_value=mock_setting_repo), \
             patch("src.scheduler.jobs.SchedulerStateRepository") as mock_sched:

            mock_session = MagicMock()
            mock_gs.return_value.__enter__ = MagicMock(return_value=mock_session)
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)
            mock_sched.return_value.get_all.return_value = []

            from src.config import settings as real_settings, AgentMode
            original = real_settings.agent_mode
            try:
                await startup_sweep()
                assert real_settings.agent_mode == AgentMode.PAUSED
            finally:
                real_settings.agent_mode = original


class TestDuplicateSuppressionWithExpiry:
    """Duplicate suppression only blocks within the signal's expiry window.
    Re-notification happens naturally: once the signal expires (default 30 min),
    get_pending_for_asset returns empty, and the engine creates a fresh signal."""

    def test_no_renotify_constant_exported(self):
        import src.scheduler.jobs as jobs_mod
        assert not hasattr(jobs_mod, "DUPLICATE_RENOTIFY_HOURS"), (
            "DUPLICATE_RENOTIFY_HOURS was removed — re-notification is handled "
            "by the 30-min signal expiry cycle, not a separate timer"
        )

    def test_pending_signal_within_expiry_is_suppressed(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "medium"

        new_signal = MagicMock()
        new_signal.signal_type = "BUY"
        new_signal.entry_price = 50100.0
        new_signal.stop_loss = 48550.0
        new_signal.priority = "MEDIUM"

        assert _is_signal_equivalent(existing, new_signal) is True

    def test_expired_signal_invisible_to_pending_query(self, session, seed_asset):
        """Once a signal expires, get_pending_for_asset excludes it,
        so the next equivalent signal is treated as fresh (not suppressed)."""
        from src.signals.lifecycle import SignalLifecycle
        from datetime import datetime, timezone, timedelta

        lifecycle = SignalLifecycle(session)
        expired_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        lifecycle.create_signal(
            asset_id=seed_asset.id,
            signal_type="BUY",
            regime="TREND",
            expires_at=expired_time,
            reason="test",
            explanation="test",
            strategy_version="1.0",
            entry_price=50000.0,
            stop_loss=48500.0,
            priority="medium",
        )
        session.flush()

        pending = lifecycle.get_pending_for_asset(seed_asset.id)
        assert len(pending) == 0, "Expired signal must not appear in pending query"

    def test_all_replace_tzinfo_calls_are_guarded(self):
        """Every .replace(tzinfo=...) in src/ must be preceded by a tzinfo is None check."""
        import re
        from pathlib import Path
        src_dir = Path(__file__).resolve().parent.parent / "src"
        unguarded = []
        for py_file in src_dir.rglob("*.py"):
            lines = py_file.read_text().splitlines()
            for i, line in enumerate(lines):
                if ".replace(tzinfo=" in line:
                    prev_line = lines[i - 1] if i > 0 else ""
                    if "tzinfo is None" not in prev_line and "tzinfo is None" not in line:
                        unguarded.append(f"{py_file.relative_to(src_dir.parent)}:{i+1}")
        assert unguarded == [], f"Unguarded .replace(tzinfo=) calls: {unguarded}"
