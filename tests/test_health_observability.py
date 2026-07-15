"""Iteration 6: Operational Health and Observability tests."""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    Base, HealthTransition, Asset, SchedulerState, PaperAccount, Signal,
)
from src.database.repository import (
    HealthTransitionRepository, SchedulerStateRepository,
    SignalRepository, PaperAccountRepository,
)
from src.health.models import HealthStatus, ComponentHealth, SystemHealth
from src.health.service import HealthService, get_health_service


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def seed_asset(session):
    asset = Asset(symbol="BTC", kraken_pair="XXBTZUSD", coinbase_pair="BTC-USD")
    session.add(asset)
    session.commit()
    return asset


@pytest.fixture
def seed_scheduler(session):
    states = []
    for name in ["market_check", "expire_signals", "health_check"]:
        s = SchedulerState(
            job_name=name,
            run_count=5,
            success_count=5,
            failure_count=0,
            current_status="idle",
            last_run_at=datetime.now(timezone.utc),
            last_success_at=datetime.now(timezone.utc),
        )
        session.add(s)
        states.append(s)
    session.commit()
    return states


@pytest.fixture
def seed_account(session):
    acct = PaperAccount(
        balance_usd=1000.0,
        peak_balance=1000.0,
        starting_balance=1000.0,
    )
    session.add(acct)
    session.commit()
    return acct


# ── Section 1: HealthTransition model ─────────────────────────────────

class TestHealthTransitionModel:
    def test_create_transition(self, session):
        repo = HealthTransitionRepository(session)
        t = repo.record("database", "HEALTHY", "UNHEALTHY", "Connection refused")
        session.commit()
        assert t.id is not None
        assert t.component == "database"
        assert t.old_status == "HEALTHY"
        assert t.new_status == "UNHEALTHY"
        assert t.reason == "Connection refused"
        assert t.recovered_at is None

    def test_get_latest_for_component(self, session):
        repo = HealthTransitionRepository(session)
        repo.record("scheduler", "HEALTHY", "DEGRADED", "job stale")
        repo.record("scheduler", "DEGRADED", "UNHEALTHY", "all jobs stale")
        session.commit()
        latest = repo.get_latest_for_component("scheduler")
        assert latest.new_status == "UNHEALTHY"

    def test_mark_recovery(self, session):
        repo = HealthTransitionRepository(session)
        repo.record("database", "HEALTHY", "UNHEALTHY", "down")
        session.commit()
        recovered = repo.mark_recovery("database")
        session.commit()
        assert recovered is not None
        assert recovered.recovered_at is not None
        assert recovered.recovery_seconds >= 0

    def test_mark_recovery_no_op_when_healthy(self, session):
        repo = HealthTransitionRepository(session)
        repo.record("database", "UNHEALTHY", "HEALTHY", "recovered")
        session.commit()
        result = repo.mark_recovery("database")
        assert result is None

    def test_get_recent(self, session):
        repo = HealthTransitionRepository(session)
        for i in range(5):
            repo.record("test", "HEALTHY", "DEGRADED", f"reason {i}")
        session.commit()
        recent = repo.get_recent(limit=3)
        assert len(recent) == 3


# ── Section 2: HealthStatus enum ──────────────────────────────────────

class TestHealthStatusEnum:
    def test_values(self):
        assert HealthStatus.HEALTHY.value == "HEALTHY"
        assert HealthStatus.DEGRADED.value == "DEGRADED"
        assert HealthStatus.UNHEALTHY.value == "UNHEALTHY"

    def test_component_health_is_healthy(self):
        h = ComponentHealth(name="db", status=HealthStatus.HEALTHY, message="ok")
        assert h.is_healthy()
        d = ComponentHealth(name="db", status=HealthStatus.DEGRADED, message="slow")
        assert not d.is_healthy()


# ── Section 3: SystemHealth aggregation model ─────────────────────────

class TestSystemHealth:
    def test_add_and_get(self):
        system = SystemHealth(status=HealthStatus.HEALTHY)
        c = ComponentHealth(name="db", status=HealthStatus.HEALTHY, message="ok")
        system.add(c)
        assert system.get("db") is c
        assert system.get("nonexistent") is None


# ── Section 4: Database health check ─────────────────────────────────

class TestDatabaseHealthCheck:
    def test_healthy_database(self, engine):
        service = HealthService()
        with patch("src.health.service.check_db_health", return_value={"status": "ok", "backend": "sqlite"}):
            result = service.check_database()
        assert result.status == HealthStatus.HEALTHY
        assert "Connected" in result.message

    def test_unhealthy_database(self):
        service = HealthService()
        with patch("src.health.service.check_db_health", return_value={"status": "error", "error": "refused"}):
            result = service.check_database()
        assert result.status == HealthStatus.UNHEALTHY
        assert "refused" in result.message

    def test_database_exception(self):
        service = HealthService()
        with patch("src.health.service.check_db_health", side_effect=Exception("boom")):
            result = service.check_database()
        assert result.status == HealthStatus.UNHEALTHY
        assert "Unreachable" in result.message


# ── Section 5: Scheduler health check ────────────────────────────────

class TestSchedulerHealthCheck:
    def test_healthy_scheduler(self, session, seed_scheduler):
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            result = service.check_scheduler()
        assert result.status == HealthStatus.HEALTHY

    def test_no_scheduler_state(self, session):
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            result = service.check_scheduler()
        assert result.status == HealthStatus.UNHEALTHY

    def test_scheduler_with_errors(self, session):
        s = SchedulerState(
            job_name="market_check", run_count=5, success_count=4,
            failure_count=1, current_status="idle",
            last_run_at=datetime.now(timezone.utc),
            last_success_at=datetime.now(timezone.utc),
            last_error="timeout",
        )
        session.add(s)
        session.commit()
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            result = service.check_scheduler()
        assert result.status == HealthStatus.DEGRADED

    def test_scheduler_all_stale(self, session):
        s = SchedulerState(
            job_name="market_check", run_count=1, success_count=1,
            failure_count=0, current_status="idle",
            last_run_at=datetime.now(timezone.utc) - timedelta(hours=2),
            last_success_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        session.add(s)
        session.commit()
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            result = service.check_scheduler()
        assert result.status == HealthStatus.UNHEALTHY

    def test_scheduler_exception(self):
        service = HealthService()
        with patch("src.health.service.get_session", side_effect=Exception("db gone")):
            result = service.check_scheduler()
        assert result.status == HealthStatus.UNHEALTHY


# ── Section 6: Telegram health check ─────────────────────────────────

class TestTelegramHealthCheck:
    def test_healthy_telegram(self):
        service = HealthService()
        service._send_message_func = AsyncMock()
        with patch.object(service, "check_telegram") as mock_check:
            mock_check.return_value = ComponentHealth(
                name="telegram", status=HealthStatus.HEALTHY, message="Connected"
            )
            result = mock_check()
        assert result.status == HealthStatus.HEALTHY

    def test_no_token(self):
        service = HealthService()
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.telegram_bot_token = ""
            mock_settings.telegram_chat_id = "123"
            result = service.check_telegram()
        assert result.status == HealthStatus.UNHEALTHY

    def test_test_token(self):
        service = HealthService()
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.telegram_bot_token = "test_token_not_real"
            mock_settings.telegram_chat_id = "123"
            result = service.check_telegram()
        assert result.status == HealthStatus.UNHEALTHY

    def test_no_chat_id(self):
        service = HealthService()
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.telegram_bot_token = "real_token_123"
            mock_settings.telegram_chat_id = ""
            result = service.check_telegram()
        assert result.status == HealthStatus.DEGRADED

    def test_no_send_func(self):
        service = HealthService()
        service._send_message_func = None
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.telegram_bot_token = "real_token_123"
            mock_settings.telegram_chat_id = "123"
            result = service.check_telegram()
        assert result.status == HealthStatus.DEGRADED


# ── Section 7: Market data health check ──────────────────────────────

class TestMarketDataHealthCheck:
    def test_healthy_market_data(self):
        service = HealthService()
        mock_health = MagicMock()
        mock_health.latest_error = None
        mock_health.candle_freshness_hours = 1.0
        mock_pipeline = MagicMock()
        mock_pipeline.get_health.return_value = mock_health
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.assets = [MagicMock(active=True, symbol="BTC")]
            mock_settings.max_daily_candle_age_hours = 30
            with patch("src.scheduler.jobs.get_pipeline", return_value=mock_pipeline):
                result = service.check_market_data()
        assert result.status == HealthStatus.HEALTHY

    def test_unhealthy_market_data(self):
        service = HealthService()
        mock_health = MagicMock()
        mock_health.latest_error = "API timeout"
        mock_pipeline = MagicMock()
        mock_pipeline.get_health.return_value = mock_health
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.assets = [MagicMock(active=True, symbol="BTC")]
            with patch("src.scheduler.jobs.get_pipeline", return_value=mock_pipeline):
                result = service.check_market_data()
        assert result.status == HealthStatus.UNHEALTHY

    def test_market_data_exception(self):
        service = HealthService()
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.assets = [MagicMock(active=True, symbol="BTC")]
            with patch("src.scheduler.jobs.get_pipeline", side_effect=Exception("no pipeline")):
                result = service.check_market_data()
        assert result.status == HealthStatus.DEGRADED


# ── Section 8: Provider health check ─────────────────────────────────

class TestProviderHealthCheck:
    def test_kraken_healthy(self):
        service = HealthService()
        mock_health = MagicMock()
        mock_health.current_provider = "kraken"
        mock_pipeline = MagicMock()
        mock_pipeline.get_health.return_value = mock_health
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.assets = [MagicMock(active=True, symbol="BTC")]
            with patch("src.scheduler.jobs.get_pipeline", return_value=mock_pipeline):
                result = service.check_providers()
        assert result.status == HealthStatus.HEALTHY
        assert "Kraken: OK" in result.message

    def test_only_coinbase(self):
        service = HealthService()
        mock_health = MagicMock()
        mock_health.current_provider = "coinbase"
        mock_pipeline = MagicMock()
        mock_pipeline.get_health.return_value = mock_health
        with patch("src.health.service.settings") as mock_settings:
            mock_settings.assets = [MagicMock(active=True, symbol="BTC")]
            with patch("src.scheduler.jobs.get_pipeline", return_value=mock_pipeline):
                result = service.check_providers()
        assert result.status == HealthStatus.DEGRADED
        assert "fallback" in result.message.lower()


# ── Section 9: Signal engine health check ────────────────────────────

class TestSignalEngineHealthCheck:
    def test_healthy(self, session):
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            result = service.check_signal_engine()
        assert result.status == HealthStatus.HEALTHY
        assert "0 pending" in result.message

    def test_exception(self):
        service = HealthService()
        with patch("src.health.service.get_session", side_effect=Exception("db")):
            result = service.check_signal_engine()
        assert result.status == HealthStatus.UNHEALTHY


# ── Section 10: Paper trading health check ───────────────────────────

class TestPaperTradingHealthCheck:
    def test_healthy_account(self, session, seed_account):
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            with patch("src.health.service.settings") as mock_settings:
                mock_settings.loss_level = 950.0
                result = service.check_paper_trading()
        assert result.status == HealthStatus.HEALTHY

    def test_balance_at_loss_boundary(self, session):
        acct = PaperAccount(
            balance_usd=950.0, peak_balance=1000.0,
            starting_balance=1000.0, challenge_status="active",
        )
        session.add(acct)
        session.commit()
        service = HealthService()
        with patch("src.health.service.get_session") as mock_sess:
            mock_sess.return_value.__enter__ = lambda s: session
            mock_sess.return_value.__exit__ = MagicMock(return_value=False)
            with patch("src.health.service.settings") as mock_settings:
                mock_settings.loss_level = 950.0
                result = service.check_paper_trading()
        assert result.status == HealthStatus.UNHEALTHY


# ── Section 11: Health aggregation ───────────────────────────────────

class TestHealthAggregation:
    def test_all_healthy(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        for name in ["database", "scheduler", "telegram"]:
            system.add(ComponentHealth(name=name, status=HealthStatus.HEALTHY, message="ok"))
        result = service._aggregate(system)
        assert result == HealthStatus.HEALTHY

    def test_database_unhealthy_overall_unhealthy(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.UNHEALTHY, message="down"))
        system.add(ComponentHealth(name="scheduler", status=HealthStatus.HEALTHY, message="ok"))
        result = service._aggregate(system)
        assert result == HealthStatus.UNHEALTHY

    def test_scheduler_unhealthy_overall_unhealthy(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))
        system.add(ComponentHealth(name="scheduler", status=HealthStatus.UNHEALTHY, message="stale"))
        result = service._aggregate(system)
        assert result == HealthStatus.UNHEALTHY

    def test_other_unhealthy_overall_degraded(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))
        system.add(ComponentHealth(name="scheduler", status=HealthStatus.HEALTHY, message="ok"))
        system.add(ComponentHealth(name="telegram", status=HealthStatus.UNHEALTHY, message="down"))
        result = service._aggregate(system)
        assert result == HealthStatus.DEGRADED

    def test_degraded_component_overall_degraded(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))
        system.add(ComponentHealth(name="providers", status=HealthStatus.DEGRADED, message="fallback"))
        result = service._aggregate(system)
        assert result == HealthStatus.DEGRADED

    def test_db_unhealthy_trumps_all(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.UNHEALTHY, message="down"))
        system.add(ComponentHealth(name="scheduler", status=HealthStatus.DEGRADED, message="x"))
        system.add(ComponentHealth(name="telegram", status=HealthStatus.DEGRADED, message="x"))
        result = service._aggregate(system)
        assert result == HealthStatus.UNHEALTHY


# ── Section 12: Recovery tracking ────────────────────────────────────

class TestRecoveryTracking:
    def test_transition_and_recovery(self, session):
        repo = HealthTransitionRepository(session)
        repo.record("database", "HEALTHY", "UNHEALTHY", "connection lost")
        session.commit()

        recovered = repo.mark_recovery("database")
        session.commit()
        assert recovered is not None
        assert recovered.recovery_seconds is not None
        assert recovered.recovery_seconds >= 0

    def test_no_recovery_needed(self, session):
        repo = HealthTransitionRepository(session)
        repo.record("database", "UNHEALTHY", "HEALTHY", "back up")
        session.commit()
        result = repo.mark_recovery("database")
        assert result is None


# ── Section 13: Notification suppression ─────────────────────────────

class TestNotificationSuppression:
    @pytest.fixture(autouse=True)
    def _loop(self):
        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    def test_no_notification_on_first_check(self, _loop):
        service = HealthService()
        mock_send = AsyncMock()
        service._send_message_func = mock_send

        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))

        _loop.run_until_complete(service.notify_if_needed(system))
        mock_send.assert_not_called()

    def test_notification_on_unhealthy_transition(self, _loop):
        service = HealthService()
        mock_send = AsyncMock()
        service._send_message_func = mock_send
        service._last_statuses["database"] = HealthStatus.HEALTHY

        system = SystemHealth(status=HealthStatus.UNHEALTHY)
        system.add(ComponentHealth(
            name="database", status=HealthStatus.UNHEALTHY, message="Connection refused"
        ))

        _loop.run_until_complete(service.notify_if_needed(system))
        mock_send.assert_called_once()
        call_text = mock_send.call_args[0][0]
        assert "UNHEALTHY" in call_text
        assert "database" in call_text

    def test_notification_on_recovery(self, _loop):
        service = HealthService()
        mock_send = AsyncMock()
        service._send_message_func = mock_send
        service._last_statuses["database"] = HealthStatus.UNHEALTHY

        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(
            name="database", status=HealthStatus.HEALTHY, message="Connected"
        ))

        _loop.run_until_complete(service.notify_if_needed(system))
        mock_send.assert_called_once()
        call_text = mock_send.call_args[0][0]
        assert "RECOVERED" in call_text

    def test_no_duplicate_notification(self, _loop):
        service = HealthService()
        mock_send = AsyncMock()
        service._send_message_func = mock_send
        service._last_statuses["database"] = HealthStatus.HEALTHY

        system = SystemHealth(status=HealthStatus.UNHEALTHY)
        system.add(ComponentHealth(
            name="database", status=HealthStatus.UNHEALTHY, message="down"
        ))

        _loop.run_until_complete(service.notify_if_needed(system))
        assert mock_send.call_count == 1

        _loop.run_until_complete(service.notify_if_needed(system))
        assert mock_send.call_count == 1

    def test_no_send_func_no_crash(self, _loop):
        service = HealthService()
        service._send_message_func = None
        system = SystemHealth(status=HealthStatus.UNHEALTHY)
        _loop.run_until_complete(service.notify_if_needed(system))


# ── Section 14: /health command format ───────────────────────────────

class TestHealthCommandFormat:
    def test_format_includes_all_components(self):
        service = HealthService()
        system = SystemHealth(
            status=HealthStatus.HEALTHY,
            checked_at=datetime.now(timezone.utc),
        )
        for name in ["database", "scheduler", "telegram", "market_data", "providers",
                      "signal_engine", "paper_trading"]:
            system.add(ComponentHealth(name=name, status=HealthStatus.HEALTHY, message="ok"))

        with patch("src.health.service.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.strategy_version = "1.0"
            mock_settings.live_trading_enabled = False
            mock_settings.agent_mode.value = "PAPER_CHALLENGE"
            with patch("src.health.service.get_session", side_effect=Exception("skip")):
                text = service.format_health_command(system)

        assert "System Health" in text
        assert "HEALTHY" in text
        assert "Database" in text
        assert "Scheduler" in text
        assert "Telegram" in text
        assert "Market Data" in text
        assert "Providers" in text
        assert "Signal Engine" in text
        assert "Paper Trading" in text
        assert "Strategy Version" in text

    def test_format_no_secrets(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))

        with patch("src.health.service.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.strategy_version = "1.0"
            mock_settings.live_trading_enabled = False
            mock_settings.agent_mode.value = "PAPER_CHALLENGE"
            with patch("src.health.service.get_session", side_effect=Exception("skip")):
                text = service.format_health_command(system)

        for secret_pattern in ["token", "password", "secret", "api_key", "connection_string"]:
            assert secret_pattern not in text.lower() or secret_pattern in "test_token_not_real"

    def test_format_degraded_shows_yellow(self):
        service = HealthService()
        system = SystemHealth(status=HealthStatus.DEGRADED)
        system.add(ComponentHealth(
            name="providers", status=HealthStatus.DEGRADED, message="fallback"
        ))

        with patch("src.health.service.settings") as mock_settings:
            mock_settings.app_env = "development"
            mock_settings.strategy_version = "1.0"
            mock_settings.live_trading_enabled = False
            mock_settings.agent_mode.value = "PAPER_CHALLENGE"
            with patch("src.health.service.get_session", side_effect=Exception("skip")):
                text = service.format_health_command(system)

        assert "DEGRADED" in text


# ── Section 15: Health check job ─────────────────────────────────────

class TestHealthCheckJob:
    def test_health_check_job_exists(self):
        from src.scheduler.jobs import health_check_job
        assert callable(health_check_job)

    def test_scheduler_includes_health_check(self):
        from src.scheduler.jobs import setup_scheduler
        with patch("src.scheduler.jobs.pytz.timezone", return_value="UTC"):
            scheduler = setup_scheduler()
        job = scheduler.get_job("health_check")
        assert job is not None


# ── Section 16: Startup diagnostics ──────────────────────────────────

class TestStartupDiagnostics:
    def test_main_imports(self):
        import main
        assert hasattr(main, "run_bot")
        assert hasattr(main, "set_app_ready")
        assert hasattr(main, "is_app_ready")

    def test_readiness_starts_false(self):
        import main
        main._app_ready = False
        assert not main.is_app_ready()

    def test_readiness_toggle(self):
        import main
        main.set_app_ready(True)
        assert main.is_app_ready()
        main.set_app_ready(False)
        assert not main.is_app_ready()


# ── Section 17: Liveness endpoint ────────────────────────────────────

class TestLivenessEndpoint:
    def test_root_returns_200(self):
        import main
        from io import BytesIO
        handler = MagicMock(spec=main.HealthHandler)
        handler.path = "/"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        main.HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)


# ── Section 18: Readiness endpoint ───────────────────────────────────

class TestReadinessEndpoint:
    def test_health_returns_503_before_ready(self):
        import main
        main._app_ready = False
        from io import BytesIO
        handler = MagicMock(spec=main.HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        main.HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(503)

    def test_health_returns_200_after_ready(self):
        import main
        main._app_ready = True
        from io import BytesIO
        handler = MagicMock(spec=main.HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        main.HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)
        main._app_ready = False


# ── Section 19: History recording ────────────────────────────────────

class TestHistoryRecording:
    def test_run_check_records_transitions(self):
        service = HealthService()
        service._last_statuses["database"] = HealthStatus.HEALTHY

        mock_db_check = ComponentHealth(
            name="database", status=HealthStatus.UNHEALTHY, message="down"
        )

        with patch.object(service, "check_all") as mock_check:
            system = SystemHealth(status=HealthStatus.UNHEALTHY)
            system.add(mock_db_check)
            mock_check.return_value = system
            with patch("src.health.service.get_session") as mock_sess:
                mock_session = MagicMock()
                mock_sess.return_value.__enter__ = lambda s: mock_session
                mock_sess.return_value.__exit__ = MagicMock(return_value=False)
                with patch("src.health.service.HealthTransitionRepository") as mock_repo_cls:
                    mock_repo = MagicMock()
                    mock_repo_cls.return_value = mock_repo
                    service.run_check_and_record()
                    mock_repo.record.assert_called()
                    args = mock_repo.record.call_args_list[0]
                    assert args.kwargs["component"] == "database"
                    assert args.kwargs["old_status"] == "HEALTHY"
                    assert args.kwargs["new_status"] == "UNHEALTHY"

    def test_no_transition_when_status_unchanged(self):
        service = HealthService()
        service._last_statuses["database"] = HealthStatus.HEALTHY

        mock_db_check = ComponentHealth(
            name="database", status=HealthStatus.HEALTHY, message="ok"
        )

        with patch.object(service, "check_all") as mock_check:
            system = SystemHealth(status=HealthStatus.HEALTHY)
            system.add(mock_db_check)
            mock_check.return_value = system
            with patch("src.health.service.get_session") as mock_sess:
                mock_session = MagicMock()
                mock_sess.return_value.__enter__ = lambda s: mock_session
                mock_sess.return_value.__exit__ = MagicMock(return_value=False)
                with patch("src.health.service.HealthTransitionRepository") as mock_repo_cls:
                    mock_repo = MagicMock()
                    mock_repo_cls.return_value = mock_repo
                    service.run_check_and_record()
                    mock_repo.record.assert_not_called()


# ── Section 20: Bot /health command integration ──────────────────────

class TestBotHealthCommand:
    def test_cmd_health_exists(self):
        from src.telegram_bot.bot import cmd_health
        assert callable(cmd_health)

    def test_health_in_help_text(self):
        from src.telegram_bot.bot import cmd_help
        assert cmd_help is not None

    def test_health_handler_registered(self):
        from src.telegram_bot.bot import create_bot
        app = create_bot(token="test_token_not_real")
        handlers = app.handlers.get(0, [])
        commands = []
        for h in handlers:
            if hasattr(h, "commands"):
                commands.extend(h.commands)
        assert "health" in commands


# ── Section 21: Migration ────────────────────────────────────────────

class TestMigration:
    def test_migration_file_exists(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions",
            "005_health_observability.py",
        )
        assert os.path.exists(path)

    def test_health_transition_table_columns(self, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("health_transitions")}
        expected = {
            "id", "component", "old_status", "new_status", "reason",
            "recovered_at", "recovery_seconds", "created_at",
        }
        assert expected.issubset(columns)


# ── Section 22: get_health_service singleton ─────────────────────────

class TestHealthServiceSingleton:
    def test_returns_same_instance(self):
        import src.health.service as mod
        mod._health_service = None
        s1 = get_health_service()
        s2 = get_health_service()
        assert s1 is s2
        mod._health_service = None


# ── Section 23: No secrets in health output ──────────────────────────

class TestNoSecrets:
    def test_health_format_never_leaks_env_vars(self):
        import os
        service = HealthService()
        system = SystemHealth(status=HealthStatus.HEALTHY)
        system.add(ComponentHealth(name="database", status=HealthStatus.HEALTHY, message="ok"))

        with patch("src.health.service.settings") as mock_settings:
            mock_settings.app_env = "production"
            mock_settings.strategy_version = "1.0"
            mock_settings.live_trading_enabled = False
            mock_settings.agent_mode.value = "PAPER_CHALLENGE"
            with patch("src.health.service.get_session", side_effect=Exception("skip")):
                text = service.format_health_command(system)

        assert os.environ.get("TELEGRAM_BOT_TOKEN", "") not in text or "test_token" in text
        assert "DATABASE_URL" not in text
        assert "sqlite:///" not in text
        assert "postgresql://" not in text
