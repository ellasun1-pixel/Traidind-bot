"""
End-to-End Smoke Test — 14-Step Simulated Flow

Simulates the full lifecycle: startup → market data → signal → confirm → report → health.
All operations are paper-only with mocked external services.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings, AgentMode
from src.database import get_session
from src.database.session import init_db
from src.database.models import Signal, Asset
from src.database.repository import (
    AssetRepository, SchedulerStateRepository, PaperAccountRepository,
    SignalRepository, AppSettingRepository,
)
from src.signals.lifecycle import SignalLifecycle, SignalType
from src.strategy.engine import TradeSignal
from src.strategy.regime import MarketRegime
from src.notifier.formatter import SignalFormatter
from src.health.service import HealthService
from src.health.models import HealthStatus, ComponentHealth


class TestEndToEndSmoke:
    @pytest.fixture(autouse=True)
    def _setup(self):
        init_db()
        self.loop = asyncio.new_event_loop()
        yield
        self.loop.close()

    def test_full_lifecycle(self):
        # Step 1: Verify environment
        assert settings.live_trading_enabled is False
        assert settings.agent_mode in (AgentMode.PAPER_CHALLENGE, AgentMode.PAUSED)

        # Step 2: Database connection
        from src.database.session import check_db_health
        db = check_db_health()
        assert db["status"] == "ok"

        # Step 3: Assets configured
        assert len(settings.assets) >= 5
        symbols = [a.symbol for a in settings.assets]
        assert "BTC/USD" in symbols
        assert "ETH/USD" in symbols

        # Step 4: Paper account initialized
        with get_session() as session:
            acct_repo = PaperAccountRepository(session)
            account = acct_repo.get_or_create()
            assert float(account.balance_usd) == settings.starting_balance

        # Step 5: Create a signal
        with get_session() as session:
            asset_repo = AssetRepository(session)
            asset_repo.upsert("BTC/USD", "XXBTZUSD", "BTC-USD")
            session.flush()
            asset = session.query(Asset).filter(Asset.symbol == "BTC/USD").first()
            asset_id = asset.id

            lifecycle = SignalLifecycle(session)
            expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            sig = lifecycle.create_signal(
                asset_id=asset_id,
                signal_type="BUY",
                regime="TREND",
                expires_at=expires,
                reason="E2E smoke test",
                explanation="Test signal",
                strategy_version="1.0",
                entry_price=50000.0,
                stop_loss=48500.0,
                position_size_usd=100.0,
                max_loss_usd=3.0,
            )
            sig_id = sig.id

        # Step 6: Signal is pending
        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            pending = lifecycle.get_pending_for_asset(asset_id)
            assert len(pending) >= 1
            assert any(p.id == sig_id for p in pending)

        # Step 7: Format signal message
        trade_signal = TradeSignal(
            signal_type="BUY",
            priority="HIGH",
            asset_symbol="BTC/USD",
            regime=MarketRegime.TREND,
            entry_price=50000.0,
            stop_loss=48500.0,
            position_size_usd=100.0,
            max_loss_usd=3.0,
            reason="E2E smoke test",
            explanation="Test signal",
            current_balance=1000.0,
            distance_to_win=120.0,
            distance_to_loss=50.0,
        )
        fmt = SignalFormatter(beginner_mode=False)
        msg = fmt.format_signal(trade_signal, signal_id=str(sig_id))
        assert "PAPER TRADE" in msg
        assert "BTC/USD" in msg
        assert str(sig_id) in msg
        assert "/confirm" in msg

        # Step 8: Confirm signal
        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            sig_obj = session.query(Signal).filter(Signal.id == sig_id).first()
            confirmed = lifecycle.confirm(sig_obj)
            assert confirmed.status == "confirmed"

        # Step 9: Signal is now confirmed
        with get_session() as session:
            sig_obj = session.query(Signal).filter(Signal.id == sig_id).first()
            assert sig_obj.status == "confirmed"

        # Step 10: Reject another signal
        with get_session() as session:
            lifecycle = SignalLifecycle(session)
            expires = datetime.now(timezone.utc) + timedelta(minutes=30)
            sig2 = lifecycle.create_signal(
                asset_id=asset_id,
                signal_type="BUY",
                regime="CHOP",
                expires_at=expires,
                reason="should be rejected",
                explanation="reject test",
                strategy_version="1.0",
            )
            rejected = lifecycle.reject(sig2)
            assert rejected.status == "rejected"
            sig2_id = sig2.id

        with get_session() as session:
            sig2_obj = session.query(Signal).filter(Signal.id == sig2_id).first()
            assert sig2_obj.status == "rejected"

        # Step 11: Format morning report
        summary = {
            "balance_usd": 1000.0, "total_equity": 1000.0,
            "realized_pnl": 0.0, "unrealized_pnl": 0.0,
            "drawdown_pct": 0.0, "peak_balance": 1000.0,
            "distance_to_win": 120.0, "distance_to_loss": 50.0,
            "challenge_status": "active", "open_positions_count": 0,
            "total_trades": 0, "open_positions": [],
        }
        morning = fmt.format_report("morning", summary, health_status="HEALTHY")
        assert "Morning Report" in morning
        assert "Paper Challenge" in morning
        assert "HEALTHY" in morning
        assert "night mode" not in morning

        # Step 12: Format evening report
        evening = fmt.format_report("evening", summary, health_status="HEALTHY")
        assert "Evening Report" in evening
        assert "Paper Challenge" in evening
        assert "night mode" in evening

        # Step 13: Health check
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
            assert len(system.components) == 7

        # Step 14: Health command format
        health_text = service.format_health_command(system)
        assert "System Health" in health_text
        assert "HEALTHY" in health_text
        assert "Live Trading" in health_text
        assert "Disabled" in health_text
