import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Asset, Signal, AuditLog
from src.signals.lifecycle import (
    SignalLifecycle, SignalStatus, SignalType,
    InvalidTransitionError, VALID_TRANSITIONS,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    asset = Asset(symbol="BTC/USD", risk_pct=0.003, max_position_usd=150.0,
                  stop_loss_pct=0.03, min_volume=0, enabled=True)
    session.add(asset)
    session.flush()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def lifecycle(db_session):
    return SignalLifecycle(db_session)


@pytest.fixture
def asset_id(db_session):
    return db_session.query(Asset).first().id


def _future(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _past(minutes=5):
    return datetime.now(timezone.utc) - timedelta(minutes=minutes)


class TestSignalCreation:
    def test_create_basic_signal(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="Price above EMA",
        )
        assert sig.id is not None
        assert len(sig.id) == 36
        assert sig.status == "pending"
        assert sig.signal_type == "BUY"
        assert sig.regime == "TREND"
        assert sig.reason == "Price above EMA"

    def test_create_signal_with_all_fields(self, lifecycle, asset_id):
        snapshot = {"kraken_price": 65000.0, "coinbase_price": 65010.0}
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="Multi-indicator confluence",
            explanation="Price is above EMA200 with strong momentum",
            strategy_version="1.0",
            entry_price=65000.0,
            stop_loss=63000.0,
            take_profit=71000.0,
            position_size_usd=100.0,
            max_loss_usd=3.0,
            confidence=0.75,
            market_snapshot=snapshot,
            priority="high",
            order_type="LIMIT",
        )
        assert sig.entry_price == 65000.0
        assert sig.stop_loss == 63000.0
        assert sig.take_profit == 71000.0
        assert float(sig.confidence) == 0.75
        assert sig.market_snapshot == snapshot
        assert sig.explanation is not None

    def test_uuid_uniqueness(self, lifecycle, asset_id):
        ids = set()
        for _ in range(50):
            sig = lifecycle.create_signal(
                asset_id=asset_id,
                signal_type=SignalType.WAIT.value,
                regime="CHOP",
                expires_at=_future(),
                reason="test",
            )
            ids.add(sig.id)
        assert len(ids) == 50

    def test_create_signal_generates_audit_log(self, lifecycle, asset_id, db_session):
        lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        logs = db_session.query(AuditLog).filter(AuditLog.action == "SIGNAL_CREATED").all()
        assert len(logs) == 1
        assert logs[0].detail["signal_type"] == "BUY"

    def test_all_signal_types_valid(self, lifecycle, asset_id):
        for st in SignalType:
            sig = lifecycle.create_signal(
                asset_id=asset_id,
                signal_type=st.value,
                regime="TREND",
                expires_at=_future(),
                reason=f"testing {st.value}",
            )
            assert sig.signal_type == st.value


class TestSignalImmutability:
    def test_reason_preserved_after_confirm(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="Original reason",
            entry_price=65000.0,
            market_snapshot={"price": 65000.0},
        )
        lifecycle.confirm(sig)
        assert sig.reason == "Original reason"
        assert sig.entry_price == 65000.0
        assert sig.market_snapshot == {"price": 65000.0}

    def test_reason_preserved_after_reject(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="Original reason",
        )
        lifecycle.reject(sig, owner_decision_note="Not now")
        assert sig.reason == "Original reason"
        assert sig.owner_decision_note == "Not now"

    def test_market_snapshot_preserved(self, lifecycle, asset_id):
        snapshot = {"kraken": 65000, "coinbase": 65010, "spread": 0.015}
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
            market_snapshot=snapshot,
        )
        lifecycle.confirm(sig)
        lifecycle.execute_paper(sig)
        assert sig.market_snapshot == snapshot


class TestStatusTransitions:
    def test_pending_to_confirmed(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        assert sig.status == "confirmed"
        assert sig.confirmed_at is not None

    def test_pending_to_rejected(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.reject(sig)
        assert sig.status == "rejected"
        assert sig.rejected_at is not None

    def test_pending_to_expired(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_past(),
            reason="test",
        )
        sig.status = "pending"
        expired = lifecycle.expire_old_signals()
        assert len(expired) == 1
        assert expired[0].status == "expired"

    def test_pending_to_superseded(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.supersede(sig, "New analysis available")
        assert sig.status == "superseded"
        assert sig.superseded_at is not None
        assert sig.superseded_reason == "New analysis available"

    def test_pending_to_cancelled(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.cancel(sig, "Owner requested cancellation")
        assert sig.status == "cancelled"
        assert sig.cancelled_at is not None

    def test_confirmed_to_executed(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        lifecycle.execute_paper(sig)
        assert sig.status == "executed_paper"
        assert sig.executed_at is not None

    def test_confirmed_to_cancelled(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        lifecycle.cancel(sig)
        assert sig.status == "cancelled"

    def test_all_valid_transitions(self, lifecycle, asset_id):
        for from_status, to_set in VALID_TRANSITIONS.items():
            for to_status in to_set:
                sig = lifecycle.create_signal(
                    asset_id=asset_id,
                    signal_type=SignalType.WAIT.value,
                    regime="CHOP",
                    expires_at=_future(),
                    reason="transition test",
                )
                sig.status = from_status.value
                lifecycle._transition(sig, to_status)
                assert sig.status == to_status.value


class TestInvalidTransitions:
    @pytest.mark.parametrize("from_status,to_status", [
        ("rejected", "confirmed"),
        ("rejected", "executed_paper"),
        ("expired", "confirmed"),
        ("expired", "executed_paper"),
        ("superseded", "confirmed"),
        ("executed_paper", "confirmed"),
        ("executed_paper", "rejected"),
        ("cancelled", "confirmed"),
        ("cancelled", "executed_paper"),
        ("confirmed", "rejected"),
        ("confirmed", "superseded"),
        ("pending", "executed_paper"),
    ])
    def test_invalid_transition_raises(self, lifecycle, asset_id, from_status, to_status):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        sig.status = from_status
        with pytest.raises(InvalidTransitionError) as exc_info:
            lifecycle._transition(sig, SignalStatus(to_status))
        assert from_status in str(exc_info.value)
        assert to_status in str(exc_info.value)


class TestSignalExpiration:
    def test_expired_signal_cannot_be_confirmed(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_past(),
            reason="test",
        )
        with pytest.raises(InvalidTransitionError):
            lifecycle.confirm(sig)
        assert sig.status == "expired"

    def test_expire_old_signals_batch(self, lifecycle, asset_id):
        for i in range(5):
            lifecycle.create_signal(
                asset_id=asset_id,
                signal_type=SignalType.BUY.value,
                regime="TREND",
                expires_at=_past(),
                reason=f"expired {i}",
            )
        lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="still valid",
        )
        expired = lifecycle.expire_old_signals()
        assert len(expired) == 5

    def test_expired_signals_remain_for_analytics(self, lifecycle, asset_id, db_session):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_past(),
            reason="historical signal",
            entry_price=65000.0,
            market_snapshot={"price": 65000.0},
        )
        lifecycle.expire_old_signals()
        found = db_session.query(Signal).filter(Signal.id == sig.id).first()
        assert found is not None
        assert found.status == "expired"
        assert found.reason == "historical signal"
        assert found.entry_price == 65000.0
        assert found.market_snapshot == {"price": 65000.0}


class TestDuplicateOperations:
    def test_double_confirm_raises(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.confirm(sig)

    def test_double_reject_raises(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.reject(sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.reject(sig)

    def test_confirm_then_reject_raises(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.reject(sig)

    def test_reject_then_confirm_raises(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.reject(sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.confirm(sig)


class TestSupersededSignals:
    def test_create_with_previous_supersedes_it(self, lifecycle, asset_id):
        first = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="first signal",
        )
        second = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="updated analysis",
            previous_signal_id=first.id,
        )
        assert first.status == "superseded"
        assert second.previous_signal_id == first.id
        assert second.status == "pending"

    def test_superseded_cannot_be_confirmed(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.supersede(sig, "Replaced")
        with pytest.raises(InvalidTransitionError):
            lifecycle.confirm(sig)


class TestSignalChain:
    def test_chain_with_three_signals(self, lifecycle, asset_id):
        first = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="signal 1",
        )
        second = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.SELL.value,
            regime="TREND",
            expires_at=_future(),
            reason="signal 2",
            previous_signal_id=first.id,
        )
        third = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.WAIT.value,
            regime="CHOP",
            expires_at=_future(),
            reason="signal 3",
            previous_signal_id=second.id,
            supersede_previous=False,
        )

        chain = lifecycle.get_signal_chain(second.id)
        assert len(chain) == 3
        assert chain[0].id == first.id
        assert chain[1].id == second.id
        assert chain[2].id == third.id

    def test_chain_single_signal(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.WAIT.value,
            regime="CHOP",
            expires_at=_future(),
            reason="standalone",
        )
        chain = lifecycle.get_signal_chain(sig.id)
        assert len(chain) == 1
        assert chain[0].id == sig.id

    def test_chain_nonexistent_returns_empty(self, lifecycle):
        chain = lifecycle.get_signal_chain("nonexistent-uuid")
        assert chain == []


class TestOwnerConfirmation:
    def test_confirm_records_owner(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig, owner_user_id=123456789, owner_decision_note="Looks good")
        assert sig.owner_user_id == 123456789
        assert sig.owner_decision_note == "Looks good"

    def test_confirm_checks_price_tolerance(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
            entry_price=65000.0,
            price_tolerance_pct=0.02,
        )
        lifecycle.confirm(sig, current_price=65500.0)
        assert sig.status == "confirmed"

    def test_confirm_rejects_excessive_price_deviation(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
            entry_price=65000.0,
            price_tolerance_pct=0.02,
        )
        with pytest.raises(ValueError, match="deviates"):
            lifecycle.confirm(sig, current_price=70000.0)
        assert sig.status == "pending"

    def test_reject_records_owner_note(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.reject(sig, owner_user_id=123, owner_decision_note="Too risky")
        assert sig.owner_decision_note == "Too risky"


class TestAuditTrail:
    def test_every_transition_audited(self, lifecycle, asset_id, db_session):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        lifecycle.execute_paper(sig)

        logs = db_session.query(AuditLog).order_by(AuditLog.id).all()
        actions = [log.action for log in logs]
        assert "SIGNAL_CREATED" in actions
        assert "SIGNAL_CONFIRMED" in actions
        assert "SIGNAL_EXECUTED_PAPER" in actions

    def test_audit_contains_signal_id(self, lifecycle, asset_id, db_session):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        log = db_session.query(AuditLog).filter(AuditLog.action == "SIGNAL_CREATED").first()
        assert log.detail["signal_id"] == sig.id

    def test_audit_records_old_status(self, lifecycle, asset_id, db_session):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.confirm(sig)
        log = db_session.query(AuditLog).filter(AuditLog.action == "SIGNAL_CONFIRMED").first()
        assert log.detail["old_status"] == "pending"

    def test_supersede_audit_logged(self, lifecycle, asset_id, db_session):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.supersede(sig, "Better analysis")
        log = db_session.query(AuditLog).filter(AuditLog.action == "SIGNAL_SUPERSEDED").first()
        assert log is not None
        assert log.detail["signal_id"] == sig.id


class TestPaperExecution:
    def test_full_lifecycle(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="Strong trend signal",
            entry_price=65000.0,
            stop_loss=63000.0,
            take_profit=71000.0,
            position_size_usd=100.0,
            max_loss_usd=3.0,
        )
        assert sig.status == "pending"
        lifecycle.confirm(sig, owner_user_id=123)
        assert sig.status == "confirmed"
        lifecycle.execute_paper(sig)
        assert sig.status == "executed_paper"

    def test_cannot_execute_pending(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        with pytest.raises(InvalidTransitionError):
            lifecycle.execute_paper(sig)

    def test_cannot_execute_rejected(self, lifecycle, asset_id):
        sig = lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="test",
        )
        lifecycle.reject(sig)
        with pytest.raises(InvalidTransitionError):
            lifecycle.execute_paper(sig)


class TestDatabasePersistence:
    def test_signal_survives_session_reopen(self, db_session):
        engine = db_session.get_bind()
        lc = SignalLifecycle(db_session)
        asset = db_session.query(Asset).first()

        sig = lc.create_signal(
            asset_id=asset.id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="persistence test",
            entry_price=65000.0,
            market_snapshot={"test": True},
        )
        sig_id = sig.id
        db_session.commit()
        db_session.close()

        Session2 = sessionmaker(bind=engine, expire_on_commit=False)
        session2 = Session2()
        found = session2.query(Signal).filter(Signal.id == sig_id).first()
        assert found is not None
        assert found.reason == "persistence test"
        assert found.entry_price == 65000.0
        assert found.market_snapshot == {"test": True}
        assert found.status == "pending"
        session2.close()

    def test_signal_history_preserved(self, db_session):
        engine = db_session.get_bind()
        lc = SignalLifecycle(db_session)
        asset = db_session.query(Asset).first()

        signals = []
        for i in range(5):
            prev_id = signals[-1].id if signals else None
            sig = lc.create_signal(
                asset_id=asset.id,
                signal_type=SignalType.BUY.value,
                regime="TREND",
                expires_at=_future(),
                reason=f"signal {i}",
                previous_signal_id=prev_id,
                supersede_previous=(i > 0),
            )
            signals.append(sig)
        db_session.commit()
        db_session.close()

        Session2 = sessionmaker(bind=engine, expire_on_commit=False)
        session2 = Session2()
        all_signals = session2.query(Signal).order_by(Signal.created_at).all()
        assert len(all_signals) == 5
        assert all_signals[0].status == "superseded"
        assert all_signals[-1].status == "pending"
        session2.close()


class TestLatestAndPending:
    def test_get_latest_for_asset(self, lifecycle, asset_id):
        for i in range(3):
            lifecycle.create_signal(
                asset_id=asset_id,
                signal_type=SignalType.WAIT.value,
                regime="CHOP",
                expires_at=_future(),
                reason=f"signal {i}",
            )
        latest = lifecycle.get_latest_for_asset(asset_id)
        assert latest.reason == "signal 2"

    def test_get_pending_excludes_expired(self, lifecycle, asset_id):
        lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_past(),
            reason="expired",
        )
        lifecycle.create_signal(
            asset_id=asset_id,
            signal_type=SignalType.BUY.value,
            regime="TREND",
            expires_at=_future(),
            reason="valid",
        )
        pending = lifecycle.get_pending_for_asset(asset_id)
        assert len(pending) == 1
        assert pending[0].reason == "valid"
