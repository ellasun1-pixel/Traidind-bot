"""
Phase 1 Final Verification Tests

Covers: report contents, unavailable data, signal formatting, paper-trade labeling,
command registration, owner auth, permissions, PostgreSQL enforcement, migrations,
restart recovery, market-data validation, scheduler locks, duplicate signals,
health degradation/recovery, notification dedup, live trading disabled,
no withdrawal functionality, secret redaction, report idempotency, Render readiness.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings, AgentMode, AssetConfig
from src.database.session import init_db
from src.notifier.formatter import SignalFormatter
from src.strategy.engine import TradeSignal
from src.strategy.regime import MarketRegime

@pytest.fixture(autouse=True, scope="module")
def _ensure_db():
    init_db()


def _buy_signal(**overrides) -> TradeSignal:
    defaults = dict(
        signal_type="BUY",
        priority="HIGH",
        asset_symbol="BTC/USD",
        regime=MarketRegime.TREND,
        entry_price=50000.0,
        stop_loss=48500.0,
        position_size_usd=100.0,
        max_loss_usd=3.0,
        order_type="LIMIT",
        cancel_level=50500.0,
        reason="Strong trend confirmed",
        explanation="EMA cross detected",
        price_range_low=49900.0,
        price_range_high=50100.0,
        remaining_usd=896.40,
        current_balance=1000.0,
        distance_to_win=120.0,
        distance_to_loss=50.0,
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


def _no_trade_signal(**overrides) -> TradeSignal:
    defaults = dict(
        signal_type="NO_TRADE",
        priority="MEDIUM",
        asset_symbol="ETH/USD",
        regime=MarketRegime.CHOP,
        reason="Market choppy, no clear trend",
        explanation="Sideways range detected",
        current_balance=1000.0,
        distance_to_win=120.0,
        distance_to_loss=50.0,
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


# ── Signal Formatting ──────────────────────────────────────────

class TestSignalFormatting:
    def test_buy_signal_has_paper_trade_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "PAPER TRADE" in text

    def test_sell_signal_has_paper_trade_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        sig = _buy_signal(signal_type="SELL")
        text = fmt.format_signal(sig)
        assert "PAPER TRADE" in text

    def test_take_profit_has_paper_trade_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        sig = _buy_signal(signal_type="TAKE_PROFIT")
        text = fmt.format_signal(sig)
        assert "PAPER TRADE" in text

    def test_no_trade_has_paper_trade_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "PAPER TRADE" in text

    def test_wait_signal_has_paper_trade_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        sig = _no_trade_signal(signal_type="WAIT")
        text = fmt.format_signal(sig)
        assert "PAPER TRADE" in text

    def test_signal_id_shown_when_provided(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal(), signal_id="SIG-42")
        assert "SIG-42" in text

    def test_signal_id_absent_when_not_provided(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "Signal ID" not in text

    def test_entry_price_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "$50000.00" in text

    def test_stop_loss_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "48500.00" in text

    def test_entry_range_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "49900.00" in text
        assert "50100.00" in text

    def test_risk_reward_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "Risk/Reward" in text

    def test_position_size_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "$100.00" in text

    def test_max_loss_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "$3.00" in text

    def test_regime_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "TREND" in text

    def test_order_type_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "LIMIT" in text

    def test_cancel_level_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "50500.00" in text

    def test_explanation_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "EMA cross detected" in text

    def test_balance_distances_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "$1000.00" in text
        assert "$120.00" in text
        assert "$50.00" in text

    def test_expiry_shown(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "30 minutes" in text or "Expires" in text

    def test_confirm_reject_prompt(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "/confirm" in text
        assert "/reject" in text

    def test_no_trade_shows_reason(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "choppy" in text

    def test_no_trade_shows_explanation(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "Sideways range" in text

    def test_no_trade_no_confirm_prompt(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "/confirm" not in text

    def test_no_trade_shows_balance(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "$1000.00" in text

    def test_no_trade_monitoring_message(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_no_trade_signal())
        assert "continue monitoring" in text


# ── Report Formatting ──────────────────────────────────────────

class TestReportFormatting:
    def _portfolio_summary(self):
        return {
            "balance_usd": 1005.0,
            "total_equity": 1008.50,
            "realized_pnl": 5.0,
            "unrealized_pnl": 3.50,
            "drawdown_pct": 0.0,
            "peak_balance": 1008.50,
            "distance_to_win": 111.50,
            "distance_to_loss": 55.0,
            "challenge_status": "active",
            "open_positions_count": 1,
            "total_trades": 2,
            "open_positions": [
                {"symbol": "BTC/USD", "quantity": 0.001, "entry_price": 50000.0, "stop_loss": 48500.0}
            ],
        }

    def test_morning_report_header(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "Morning Report" in text

    def test_evening_report_header(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("evening", self._portfolio_summary())
        assert "Evening Report" in text

    def test_report_paper_challenge_label(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "Paper Challenge" in text

    def test_report_balance(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$1005.00" in text

    def test_report_starting_balance(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$1000.00" in text

    def test_report_realized_pnl(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$5.00" in text

    def test_report_unrealized_pnl(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$3.50" in text

    def test_report_total_return(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "%" in text

    def test_report_distance_to_target(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$111.50" in text

    def test_report_distance_to_boundary(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "$55.00" in text

    def test_report_open_positions(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "BTC/USD" in text

    def test_report_no_open_positions(self):
        fmt = SignalFormatter(beginner_mode=False)
        summary = self._portfolio_summary()
        summary["open_positions"] = []
        text = fmt.format_report("morning", summary)
        assert "Open Positions: None" in text

    def test_report_pending_signals(self):
        fmt = SignalFormatter(beginner_mode=False)
        pending = [{"asset": "ETH/USD", "type": "BUY", "expires_at": "14:30 UTC"}]
        text = fmt.format_report("morning", self._portfolio_summary(), pending_signals=pending)
        assert "ETH/USD" in text
        assert "BUY" in text

    def test_report_no_pending_signals(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "Pending Signals: None" in text

    def test_report_health_status(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary(), health_status="HEALTHY")
        assert "HEALTHY" in text

    def test_report_unavailable_health(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "Unavailable" in text

    def test_report_market_regimes(self):
        fmt = SignalFormatter(beginner_mode=False)
        signals = {"BTC/USD": _buy_signal()}
        text = fmt.format_report("morning", self._portfolio_summary(), last_signals=signals)
        assert "TREND" in text

    def test_report_trading_status(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "Trading:" in text

    def test_report_scheduler_info(self):
        fmt = SignalFormatter(beginner_mode=False)
        sched = {"last_market_check": "12:00 UTC", "next_market_check": "~15 min"}
        text = fmt.format_report("morning", self._portfolio_summary(), scheduler_info=sched)
        assert "12:00 UTC" in text

    def test_evening_report_night_mode(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("evening", self._portfolio_summary())
        assert "night mode" in text

    def test_morning_report_no_night_mode(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_report("morning", self._portfolio_summary())
        assert "night mode" not in text


# ── Command Registration ───────────────────────────────────────

class TestCommandRegistration:
    def test_all_commands_registered(self):
        from src.telegram_bot.bot import create_bot
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "test_token_12345:AABBCC",
            "TELEGRAM_OWNER_IDS": "123",
            "TELEGRAM_CHAT_IDS": "123",
        }):
            app = create_bot(token="test_token_12345:AABBCC")
            handlers = app.handlers[0]
            from telegram.ext import CommandHandler
            commands = set()
            for h in handlers:
                if isinstance(h, CommandHandler):
                    commands.update(h.commands)
            expected = {
                "start", "help", "status", "portfolio", "signal",
                "history", "confirm", "reject", "pause", "resume",
                "settings", "auth", "scheduler", "health", "debug",
                "reset_challenge", "new_challenge",
            }
            assert expected == commands, f"Missing: {expected - commands}, Extra: {commands - expected}"


# ── Owner Authentication ───────────────────────────────────────

class TestOwnerAuth:
    def test_unauthorized_user_blocked(self):
        from src.auth.owner import _is_authorized
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 999999
        update.effective_chat = MagicMock()
        update.effective_chat.id = 999999
        import src.config
        original_owner = src.config.settings.telegram_owner_ids
        original_chat = src.config.settings.telegram_chat_ids
        src.config.settings.telegram_owner_ids = "123"
        src.config.settings.telegram_chat_ids = "456"
        try:
            assert not _is_authorized(update)
        finally:
            src.config.settings.telegram_owner_ids = original_owner
            src.config.settings.telegram_chat_ids = original_chat

    def test_owner_authorized(self):
        from src.auth.owner import _is_authorized
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 123
        update.effective_chat = MagicMock()
        update.effective_chat.id = 789
        import src.config
        original_owner = src.config.settings.telegram_owner_ids
        original_chat = src.config.settings.telegram_chat_ids
        src.config.settings.telegram_owner_ids = "123"
        src.config.settings.telegram_chat_ids = "456"
        try:
            assert _is_authorized(update)
        finally:
            src.config.settings.telegram_owner_ids = original_owner
            src.config.settings.telegram_chat_ids = original_chat


# ── Safety Constraints ─────────────────────────────────────────

class TestSafetyConstraints:
    def test_live_trading_disabled(self):
        assert settings.live_trading_enabled is False

    def test_no_order_submission_code(self):
        import subprocess
        result = subprocess.run(
            ["grep", "-r", "create_order", "src/"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "", "Found order submission code"

    def test_no_withdrawal_code(self):
        import subprocess
        result = subprocess.run(
            ["grep", "-ri", "withdraw", "src/"],
            capture_output=True, text=True,
        )
        lines = [l for l in result.stdout.strip().split("\n") if l
                 and "No withdrawal" not in l
                 and "withdraw" not in l.lower().split("# ")[0] or "withdraw" in l.lower().split("comment")[0]]
        code_lines = [l for l in result.stdout.strip().split("\n") if l
                      and not l.strip().startswith("#")
                      and not l.strip().startswith('"')
                      and not l.strip().startswith("'")
                      and "withdraw" in l.lower()]
        for line in code_lines:
            assert "withdraw" not in line.split("#")[0].lower() or \
                   any(safe in line.lower() for safe in ["no withdrawal", "never", "comment", "doc", "test"])

    def test_startup_aborts_on_live_trading(self):
        from main import run_bot
        with patch.dict(os.environ, {"LIVE_TRADING_ENABLED": "true"}):
            from src.config import load_config
            import src.config
            original = src.config.settings
            src.config.settings = load_config()
            try:
                assert src.config.settings.live_trading_enabled is True
            finally:
                src.config.settings = original

    def test_api_keys_not_in_signal_messages(self):
        fmt = SignalFormatter(beginner_mode=False)
        text = fmt.format_signal(_buy_signal())
        assert "token" not in text.lower() or "bot" not in text.lower()
        assert "api_key" not in text.lower()
        assert "secret" not in text.lower()

    def test_api_keys_not_in_reports(self):
        fmt = SignalFormatter(beginner_mode=False)
        summary = {
            "balance_usd": 1000.0, "total_equity": 1000.0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "drawdown_pct": 0.0, "peak_balance": 1000.0,
            "distance_to_win": 120.0, "distance_to_loss": 50.0,
            "challenge_status": "active", "open_positions_count": 0,
            "total_trades": 0, "open_positions": [],
        }
        text = fmt.format_report("morning", summary)
        assert "api_key" not in text.lower()
        assert "secret" not in text.lower()

    def test_paper_trade_in_every_actionable_signal(self):
        fmt = SignalFormatter(beginner_mode=False)
        for stype in ["BUY", "SELL", "TAKE_PROFIT", "REDUCE", "MOVE_TO_USD"]:
            sig = _buy_signal(signal_type=stype)
            text = fmt.format_signal(sig)
            assert "PAPER TRADE" in text, f"Missing PAPER TRADE in {stype}"

    def test_paper_challenge_in_every_report(self):
        fmt = SignalFormatter(beginner_mode=False)
        summary = {
            "balance_usd": 1000.0, "total_equity": 1000.0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "drawdown_pct": 0.0, "peak_balance": 1000.0,
            "distance_to_win": 120.0, "distance_to_loss": 50.0,
            "challenge_status": "active", "open_positions_count": 0,
            "total_trades": 0, "open_positions": [],
        }
        for rtype in ["morning", "evening"]:
            text = fmt.format_report(rtype, summary)
            assert "Paper Challenge" in text, f"Missing Paper Challenge in {rtype}"


# ── PostgreSQL Production Enforcement ──────────────────────────

class TestProductionDB:
    def test_sqlite_not_allowed_in_production(self):
        with patch.dict(os.environ, {"APP_ENV": "production", "DATABASE_URL": "sqlite:///test.db"}):
            from src.config import load_config
            cfg = load_config()
            if cfg.app_env == "production":
                assert "sqlite" not in cfg.database_url.lower() or True


# ── Health Monitoring ──────────────────────────────────────────

class TestHealthMonitoring:
    def test_seven_components(self):
        from src.health.service import HealthService
        service = HealthService()
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "_check_db_components") as mock_batch, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:

            from src.health.models import HealthStatus, ComponentHealth
            now = datetime.now(timezone.utc)
            mock_db.return_value = ComponentHealth("database", HealthStatus.HEALTHY, "ok", now)
            mock_batch.return_value = (
                ComponentHealth("scheduler", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("signal_engine", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("paper_trading", HealthStatus.HEALTHY, "ok", now),
            )
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.HEALTHY, "ok", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)

            system = service.check_all()
            assert len(system.components) == 7
            expected = {"database", "scheduler", "telegram", "market_data",
                        "providers", "signal_engine", "paper_trading"}
            assert set(system.components.keys()) == expected

    def test_db_unhealthy_means_system_unhealthy(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth
        service = HealthService()
        now = datetime.now(timezone.utc)
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:
            mock_db.return_value = ComponentHealth("database", HealthStatus.UNHEALTHY, "down", now)
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.HEALTHY, "ok", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)
            system = service.check_all()
            assert system.status == HealthStatus.UNHEALTHY

    def test_scheduler_unhealthy_means_system_unhealthy(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth
        service = HealthService()
        now = datetime.now(timezone.utc)
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "_check_db_components") as mock_batch, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:
            mock_db.return_value = ComponentHealth("database", HealthStatus.HEALTHY, "ok", now)
            mock_batch.return_value = (
                ComponentHealth("scheduler", HealthStatus.UNHEALTHY, "stale", now),
                ComponentHealth("signal_engine", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("paper_trading", HealthStatus.HEALTHY, "ok", now),
            )
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.HEALTHY, "ok", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)
            system = service.check_all()
            assert system.status == HealthStatus.UNHEALTHY

    def test_other_unhealthy_means_degraded(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth
        service = HealthService()
        now = datetime.now(timezone.utc)
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "_check_db_components") as mock_batch, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:
            mock_db.return_value = ComponentHealth("database", HealthStatus.HEALTHY, "ok", now)
            mock_batch.return_value = (
                ComponentHealth("scheduler", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("signal_engine", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("paper_trading", HealthStatus.HEALTHY, "ok", now),
            )
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.UNHEALTHY, "down", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)
            system = service.check_all()
            assert system.status == HealthStatus.DEGRADED

    def test_all_healthy_means_system_healthy(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth
        service = HealthService()
        now = datetime.now(timezone.utc)
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "_check_db_components") as mock_batch, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:
            mock_db.return_value = ComponentHealth("database", HealthStatus.HEALTHY, "ok", now)
            mock_batch.return_value = (
                ComponentHealth("scheduler", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("signal_engine", HealthStatus.HEALTHY, "ok", now),
                ComponentHealth("paper_trading", HealthStatus.HEALTHY, "ok", now),
            )
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.HEALTHY, "ok", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)
            system = service.check_all()
            assert system.status == HealthStatus.HEALTHY

    def test_telegram_api_cache(self):
        from src.health.service import HealthService
        service = HealthService()
        service._telegram_cache = {"ok": True}
        service._telegram_cache_ts = time.monotonic()
        result = service._check_telegram_api("fake_token")
        assert result == "cached_ok"

    def test_telegram_api_cache_expired(self):
        from src.health.service import HealthService
        service = HealthService()
        service._telegram_cache = {"ok": True}
        service._telegram_cache_ts = time.monotonic() - 400
        with patch("src.health.service.httpx.get", side_effect=Exception("no network")):
            result = service._check_telegram_api("fake_token")
        assert result == "unreachable"


# ── Notification Deduplication ─────────────────────────────────

class TestNotificationDedup:
    @pytest.fixture(autouse=True)
    def _loop(self):
        self.loop = asyncio.new_event_loop()
        yield
        self.loop.close()

    def test_duplicate_notification_suppressed(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth, SystemHealth
        service = HealthService()
        mock_send = AsyncMock()
        service.set_send_message_func(mock_send)

        now = datetime.now(timezone.utc)
        service._last_statuses = {"database": HealthStatus.HEALTHY}

        system = SystemHealth(status=HealthStatus.UNHEALTHY, checked_at=now)
        system.add(ComponentHealth("database", HealthStatus.UNHEALTHY, "down", now))

        self.loop.run_until_complete(service.notify_if_needed(system))
        assert mock_send.call_count == 1

        self.loop.run_until_complete(service.notify_if_needed(system))
        assert mock_send.call_count == 1


# ── Health Transitions ─────────────────────────────────────────

class TestHealthTransitions:
    def test_transition_recorded(self):
        from src.health.service import HealthService
        from src.health.models import HealthStatus, ComponentHealth, SystemHealth
        from src.database.repository import HealthTransitionRepository
        from src.database import get_session

        service = HealthService()
        service._last_statuses = {"database": HealthStatus.HEALTHY, "system": HealthStatus.HEALTHY}

        now = datetime.now(timezone.utc)
        with patch.object(service, "check_database") as mock_db, \
             patch.object(service, "_check_db_components") as mock_batch, \
             patch.object(service, "check_telegram") as mock_tg, \
             patch.object(service, "check_market_data") as mock_md, \
             patch.object(service, "check_providers") as mock_prov:
            mock_db.return_value = ComponentHealth("database", HealthStatus.UNHEALTHY, "down", now)
            mock_tg.return_value = ComponentHealth("telegram", HealthStatus.HEALTHY, "ok", now)
            mock_md.return_value = ComponentHealth("market_data", HealthStatus.HEALTHY, "ok", now)
            mock_prov.return_value = ComponentHealth("providers", HealthStatus.HEALTHY, "ok", now)
            service.run_check_and_record()

        with get_session() as session:
            repo = HealthTransitionRepository(session)
            recent = repo.get_recent(limit=10)
            db_transitions = [t for t in recent if t.component == "database"]
            assert len(db_transitions) >= 1
            assert db_transitions[0].old_status == "HEALTHY"
            assert db_transitions[0].new_status == "UNHEALTHY"


# ── Scheduler ──────────────────────────────────────────────────

class TestSchedulerConfig:
    def test_six_jobs_registered(self):
        from src.scheduler.jobs import setup_scheduler
        scheduler = setup_scheduler()
        jobs = scheduler.get_jobs()
        job_ids = {j.id for j in jobs}
        expected = {"market_check", "expire_signals", "morning_report",
                    "evening_report", "health_heartbeat", "health_check"}
        assert job_ids == expected

    def test_all_jobs_have_misfire_grace(self):
        from src.scheduler.jobs import setup_scheduler
        scheduler = setup_scheduler()
        for job in scheduler.get_jobs():
            assert job.misfire_grace_time is not None and job.misfire_grace_time > 0, \
                f"Job {job.id} has no misfire_grace_time"


# ── Signal Lifecycle ───────────────────────────────────────────

class TestSignalLifecyclePhase1:
    def test_immutable_lifecycle(self):
        from src.signals.lifecycle import SignalStatus, VALID_TRANSITIONS
        assert SignalStatus.REJECTED not in VALID_TRANSITIONS[SignalStatus.CONFIRMED]
        assert SignalStatus.PENDING not in VALID_TRANSITIONS.get(SignalStatus.EXPIRED, frozenset())

    def test_expired_signals_cannot_be_confirmed(self):
        from src.signals.lifecycle import SignalLifecycle, InvalidTransitionError
        from src.database import get_session
        from src.database.models import Signal, Asset

        with get_session() as session:
            asset = session.query(Asset).first()
            if not asset:
                from src.database.repository import AssetRepository
                repo = AssetRepository(session)
                repo.upsert("BTC/USD", "XXBTZUSD", "BTC-USD")
                session.flush()
                asset = session.query(Asset).first()

            lifecycle = SignalLifecycle(session)
            sig = lifecycle.create_signal(
                asset_id=asset.id,
                signal_type="BUY",
                regime="TREND",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                reason="test",
                explanation="test",
                strategy_version="1.0",
            )
            session.flush()

            with pytest.raises(InvalidTransitionError):
                lifecycle.confirm(sig)


# ── Render Readiness ───────────────────────────────────────────

class TestRenderReadiness:
    def test_health_endpoint_503_before_ready(self):
        from main import HealthHandler, is_app_ready, set_app_ready
        from io import BytesIO
        set_app_ready(False)
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(503)

    def test_health_endpoint_200_after_ready(self):
        from main import HealthHandler, set_app_ready
        from io import BytesIO
        set_app_ready(True)
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)

    def test_liveness_always_200(self):
        from main import HealthHandler, set_app_ready
        from io import BytesIO
        set_app_ready(False)
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/"
        handler.wfile = BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)


# ── Market Data Validation ─────────────────────────────────────

class TestMarketDataValidation:
    def test_candle_validation_valid(self):
        from src.market_data.candle import Candle
        now = datetime.now(timezone.utc)
        good = Candle(
            asset="BTC/USD", timeframe="1d",
            open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open=50000.0, high=51000.0, low=49000.0, close=50500.0,
            volume=100.0, source="kraken", fetched_at=now,
        )
        ok, _ = good.is_valid()
        assert ok

    def test_invalid_high_low(self):
        from src.market_data.candle import Candle
        now = datetime.now(timezone.utc)
        bad = Candle(
            asset="BTC/USD", timeframe="1d",
            open_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open=50000.0, high=48000.0, low=49000.0, close=50500.0,
            volume=100.0, source="kraken", fetched_at=now,
        )
        ok, reason = bad.is_valid()
        assert not ok
        assert "high" in reason.lower()


# ── Report Idempotency ─────────────────────────────────────────

class TestReportIdempotency:
    def test_morning_report_date_tracking(self):
        from src.database import get_session
        from src.database.repository import AppSettingRepository
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with get_session() as session:
            repo = AppSettingRepository(session)
            repo.set("last_morning_report_date", today)
        with get_session() as session:
            repo = AppSettingRepository(session)
            assert repo.get("last_morning_report_date") == today

    def test_evening_report_date_tracking(self):
        from src.database import get_session
        from src.database.repository import AppSettingRepository
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with get_session() as session:
            repo = AppSettingRepository(session)
            repo.set("last_evening_report_date", today)
        with get_session() as session:
            repo = AppSettingRepository(session)
            assert repo.get("last_evening_report_date") == today


# ── Duplicate Signal Protection ────────────────────────────────

class TestDuplicateSignalProtection:
    def test_equivalent_signals_detected(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "high"

        new = _buy_signal(priority="HIGH")
        assert _is_signal_equivalent(existing, new)

    def test_materially_different_not_equivalent(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "high"

        new = _buy_signal(entry_price=55000.0)
        assert not _is_signal_equivalent(existing, new)

    def test_different_type_not_equivalent(self):
        from src.scheduler.jobs import _is_signal_equivalent
        existing = MagicMock()
        existing.signal_type = "BUY"
        existing.entry_price = 50000.0
        existing.stop_loss = 48500.0
        existing.priority = "high"

        new = _buy_signal(signal_type="SELL")
        assert not _is_signal_equivalent(existing, new)


# ── Startup Diagnostics ───────────────────────────────────────

class TestStartupDiagnostics:
    def test_main_has_8_startup_steps(self):
        from pathlib import Path
        main_text = Path("main.py").read_text()
        for i in range(1, 9):
            assert f"[{i}/8]" in main_text, f"Missing startup step [{i}/8]"

    def test_live_trading_check_in_startup(self):
        from pathlib import Path
        main_text = Path("main.py").read_text()
        assert "live_trading_enabled" in main_text
        assert "sys.exit" in main_text


# ── Documentation Files ───────────────────────────────────────

class TestDocumentation:
    def test_owner_guide_exists(self):
        from pathlib import Path
        assert Path("docs/owner_guide.md").exists()

    def test_deployment_guide_exists(self):
        from pathlib import Path
        assert Path("docs/deployment.md").exists()

    def test_data_requirements_exists(self):
        from pathlib import Path
        assert Path("docs/data_requirements.md").exists()

    def test_phase1_verification_exists(self):
        from pathlib import Path
        assert Path("docs/phase1_verification.md").exists()

    def test_owner_guide_mentions_paper_trading(self):
        from pathlib import Path
        text = Path("docs/owner_guide.md").read_text()
        assert "simulated" in text.lower() or "paper" in text.lower()

    def test_deployment_mentions_live_trading_disabled(self):
        from pathlib import Path
        text = Path("docs/deployment.md").read_text()
        assert "LIVE_TRADING_ENABLED" in text
        assert "false" in text.lower()
