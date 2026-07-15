from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from src.database.models import Signal, AuditLog

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    REDUCE = "REDUCE"
    TAKE_PROFIT = "TAKE_PROFIT"
    MOVE_TO_USD = "MOVE_TO_USD"


class SignalStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    EXECUTED_PAPER = "executed_paper"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: dict[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.PENDING: frozenset({
        SignalStatus.CONFIRMED,
        SignalStatus.REJECTED,
        SignalStatus.EXPIRED,
        SignalStatus.SUPERSEDED,
        SignalStatus.CANCELLED,
    }),
    SignalStatus.CONFIRMED: frozenset({
        SignalStatus.EXECUTED_PAPER,
        SignalStatus.CANCELLED,
    }),
    SignalStatus.REJECTED: frozenset(),
    SignalStatus.EXPIRED: frozenset(),
    SignalStatus.SUPERSEDED: frozenset(),
    SignalStatus.EXECUTED_PAPER: frozenset(),
    SignalStatus.CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, signal_id: str, from_status: str, to_status: str):
        self.signal_id = signal_id
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Invalid signal transition: {from_status} -> {to_status} "
            f"(signal {signal_id})"
        )


class SignalLifecycle:
    def __init__(self, session: Session):
        self.session = session

    def _validate_transition(self, signal: Signal, new_status: SignalStatus) -> None:
        current = SignalStatus(signal.status)
        allowed = VALID_TRANSITIONS.get(current, frozenset())
        if new_status not in allowed:
            raise InvalidTransitionError(signal.id, signal.status, new_status.value)

    def _audit(self, action: str, signal: Signal, detail: dict | None = None) -> None:
        entry = AuditLog(
            action=action,
            actor="system",
            detail={
                "signal_id": signal.id,
                "asset_id": signal.asset_id,
                "signal_type": signal.signal_type,
                "status": signal.status,
                **(detail or {}),
            },
        )
        self.session.add(entry)

    def create_signal(
        self,
        asset_id: int,
        signal_type: str,
        regime: str,
        expires_at: datetime,
        reason: str,
        strategy_version: str = "1.0",
        entry_price: float | Decimal | None = None,
        stop_loss: float | Decimal | None = None,
        take_profit: float | Decimal | None = None,
        position_size_usd: float | Decimal | None = None,
        max_loss_usd: float | Decimal | None = None,
        confidence: float | Decimal | None = None,
        market_snapshot: dict | None = None,
        explanation: str | None = None,
        priority: str = "normal",
        order_type: str | None = None,
        cancel_level: float | Decimal | None = None,
        price_range_low: float | Decimal | None = None,
        price_range_high: float | Decimal | None = None,
        price_tolerance_pct: float | Decimal | None = None,
        previous_signal_id: str | None = None,
        supersede_previous: bool = True,
    ) -> Signal:
        signal = Signal(
            asset_id=asset_id,
            signal_type=signal_type,
            regime=regime,
            expires_at=expires_at,
            reason=reason,
            explanation=explanation,
            strategy_version=strategy_version,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size_usd=position_size_usd,
            max_loss_usd=max_loss_usd,
            confidence=confidence,
            market_snapshot=market_snapshot,
            priority=priority,
            order_type=order_type,
            cancel_level=cancel_level,
            price_range_low=price_range_low,
            price_range_high=price_range_high,
            price_tolerance_pct=price_tolerance_pct,
            previous_signal_id=previous_signal_id,
            status=SignalStatus.PENDING.value,
        )
        self.session.add(signal)
        self.session.flush()

        if supersede_previous and previous_signal_id:
            prev = self.session.query(Signal).filter(Signal.id == previous_signal_id).first()
            if prev and prev.status == SignalStatus.PENDING.value:
                self._transition(prev, SignalStatus.SUPERSEDED, superseded_reason="New signal created")

        self._audit("SIGNAL_CREATED", signal)
        return signal

    def _transition(self, signal: Signal, new_status: SignalStatus, **kwargs) -> Signal:
        old_status = signal.status
        self._validate_transition(signal, new_status)
        signal.status = new_status.value

        now = datetime.now(timezone.utc)
        if new_status == SignalStatus.CONFIRMED:
            signal.confirmed_at = now
        elif new_status == SignalStatus.REJECTED:
            signal.rejected_at = now
        elif new_status == SignalStatus.EXPIRED:
            pass
        elif new_status == SignalStatus.SUPERSEDED:
            signal.superseded_at = now
            if "superseded_reason" in kwargs:
                signal.superseded_reason = kwargs["superseded_reason"]
        elif new_status == SignalStatus.EXECUTED_PAPER:
            signal.executed_at = now
        elif new_status == SignalStatus.CANCELLED:
            signal.cancelled_at = now

        if "owner_decision_note" in kwargs:
            signal.owner_decision_note = kwargs["owner_decision_note"]
        if "owner_user_id" in kwargs:
            signal.owner_user_id = kwargs["owner_user_id"]

        self.session.flush()
        self._audit(
            f"SIGNAL_{new_status.value.upper()}",
            signal,
            {"old_status": old_status},
        )
        return signal

    def confirm(
        self,
        signal: Signal,
        owner_user_id: int | None = None,
        owner_decision_note: str | None = None,
        current_price: float | Decimal | None = None,
    ) -> Signal:
        now = datetime.now(timezone.utc)
        expires = signal.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            self._transition(signal, SignalStatus.EXPIRED)
            raise InvalidTransitionError(signal.id, "expired", "confirmed")

        if current_price is not None and signal.entry_price is not None:
            tolerance = float(signal.price_tolerance_pct or 0.02)
            price_diff = abs(float(current_price) - float(signal.entry_price)) / float(signal.entry_price)
            if price_diff > tolerance:
                raise ValueError(
                    f"Current price {current_price} deviates {price_diff:.2%} from "
                    f"entry price {signal.entry_price} (tolerance: {tolerance:.2%})"
                )

        return self._transition(
            signal, SignalStatus.CONFIRMED,
            owner_user_id=owner_user_id,
            owner_decision_note=owner_decision_note,
        )

    def reject(
        self,
        signal: Signal,
        owner_user_id: int | None = None,
        owner_decision_note: str | None = None,
    ) -> Signal:
        return self._transition(
            signal, SignalStatus.REJECTED,
            owner_user_id=owner_user_id,
            owner_decision_note=owner_decision_note,
        )

    def cancel(self, signal: Signal, reason: str | None = None) -> Signal:
        return self._transition(
            signal, SignalStatus.CANCELLED,
            owner_decision_note=reason,
        )

    def execute_paper(self, signal: Signal) -> Signal:
        return self._transition(signal, SignalStatus.EXECUTED_PAPER)

    def supersede(self, signal: Signal, reason: str) -> Signal:
        return self._transition(
            signal, SignalStatus.SUPERSEDED,
            superseded_reason=reason,
        )

    def expire_old_signals(self) -> list[Signal]:
        now = datetime.now(timezone.utc)
        pending = (
            self.session.query(Signal)
            .filter(Signal.status == SignalStatus.PENDING.value)
            .filter(Signal.expires_at <= now)
            .all()
        )
        expired = []
        for sig in pending:
            self._transition(sig, SignalStatus.EXPIRED)
            expired.append(sig)
        return expired

    def get_signal_chain(self, signal_id: str) -> list[Signal]:
        chain = []
        current = self.session.query(Signal).filter(Signal.id == signal_id).first()
        if current is None:
            return chain

        while current.previous_signal_id:
            prev = self.session.query(Signal).filter(Signal.id == current.previous_signal_id).first()
            if prev is None:
                break
            chain.insert(0, prev)
            current = prev

        current = self.session.query(Signal).filter(Signal.id == signal_id).first()
        chain.append(current)

        while True:
            nxt = (
                self.session.query(Signal)
                .filter(Signal.previous_signal_id == current.id)
                .first()
            )
            if nxt is None:
                break
            chain.append(nxt)
            current = nxt

        return chain

    def get_latest_for_asset(self, asset_id: int) -> Optional[Signal]:
        return (
            self.session.query(Signal)
            .filter(Signal.asset_id == asset_id)
            .order_by(Signal.created_at.desc())
            .first()
        )

    def get_pending_for_asset(self, asset_id: int) -> list[Signal]:
        now = datetime.now(timezone.utc)
        return (
            self.session.query(Signal)
            .filter(
                Signal.asset_id == asset_id,
                Signal.status == SignalStatus.PENDING.value,
                Signal.expires_at > now,
            )
            .order_by(Signal.created_at.desc())
            .all()
        )
