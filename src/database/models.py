from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, Float, Text, Boolean, DateTime, JSON, Date,
    ForeignKey, UniqueConstraint, Numeric, func, String,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc)


def _genuuid():
    return str(uuid.uuid4())


class Asset(Base):
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, unique=True)
    kraken_pair = Column(String(20))
    coinbase_pair = Column(String(20))
    risk_pct = Column(Numeric(6, 4), nullable=False, default=0.003)
    max_position_usd = Column(Numeric(12, 2), nullable=False, default=150.0)
    stop_loss_pct = Column(Numeric(6, 4), nullable=False, default=0.03)
    min_volume = Column(Numeric(18, 2), nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    signals = relationship("Signal", back_populates="asset")
    positions = relationship("PaperPosition", back_populates="asset")


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    timeframe = Column(String(10), nullable=False)
    open_time = Column(DateTime(timezone=True), nullable=False)
    open = Column(Numeric(18, 8), nullable=False)
    high = Column(Numeric(18, 8), nullable=False)
    low = Column(Numeric(18, 8), nullable=False)
    close = Column(Numeric(18, 8), nullable=False)
    volume = Column(Numeric(18, 8), nullable=False)
    source = Column(String(20), nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "open_time", name="uq_price_candle"),
    )


class Signal(Base):
    __tablename__ = "signals"

    id = Column(String(36), primary_key=True, default=_genuuid)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    strategy_version = Column(String(20), nullable=False, default="1.0")
    signal_type = Column(String(20), nullable=False)
    priority = Column(String(10), nullable=False, default="normal")
    regime = Column(String(20), nullable=False)
    entry_price = Column(Numeric(18, 8))
    stop_loss = Column(Numeric(18, 8))
    take_profit = Column(Numeric(18, 8))
    position_size_usd = Column(Numeric(12, 2))
    max_loss_usd = Column(Numeric(12, 2))
    order_type = Column(String(10))
    cancel_level = Column(Numeric(18, 8))
    reason = Column(Text)
    explanation = Column(Text)
    confidence = Column(Numeric(5, 4))
    market_snapshot = Column(JSON)
    price_range_low = Column(Numeric(18, 8))
    price_range_high = Column(Numeric(18, 8))
    price_tolerance_pct = Column(Numeric(6, 4), default=0.02)
    status = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    confirmed_at = Column(DateTime(timezone=True))
    rejected_at = Column(DateTime(timezone=True))
    executed_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    superseded_at = Column(DateTime(timezone=True))
    owner_user_id = Column(Integer)
    owner_decision_note = Column(Text)
    previous_signal_id = Column(String(36), ForeignKey("signals.id"))
    superseded_reason = Column(Text)

    asset = relationship("Asset", back_populates="signals")
    position = relationship("PaperPosition", back_populates="signal", uselist=False)
    previous_signal = relationship("Signal", remote_side=[id], foreign_keys=[previous_signal_id])


class PaperAccount(Base):
    __tablename__ = "paper_account"

    id = Column(Integer, primary_key=True, autoincrement=True)
    balance_usd = Column(Numeric(12, 2), nullable=False, default=1000.00)
    peak_balance = Column(Numeric(12, 2), nullable=False, default=1000.00)
    starting_balance = Column(Numeric(12, 2), nullable=False, default=1000.00)
    realized_pnl = Column(Numeric(12, 2), nullable=False, default=0.00)
    daily_loss = Column(Numeric(12, 2), nullable=False, default=0.00)
    daily_loss_date = Column(Date, nullable=False, default=lambda: datetime.now(timezone.utc).date())
    challenge_status = Column(String(10), nullable=False, default="active")
    strategy_version = Column(String(20), nullable=False, default="1.0")
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    signal_id = Column(String(36), ForeignKey("signals.id"))
    side = Column(String(4), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=False)
    entry_price = Column(Numeric(18, 8), nullable=False)
    stop_loss = Column(Numeric(18, 8), nullable=False)
    take_profit = Column(Numeric(18, 8))
    opened_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    closed_at = Column(DateTime(timezone=True))
    exit_price = Column(Numeric(18, 8))
    realized_pnl = Column(Numeric(12, 2))
    close_reason = Column(String(20))
    is_open = Column(Boolean, nullable=False, default=True)

    asset = relationship("Asset", back_populates="positions")
    signal = relationship("Signal", back_populates="position")


class TradeHistory(Base):
    __tablename__ = "trade_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("paper_positions.id"))
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    signal_id = Column(String(36), ForeignKey("signals.id"))
    side = Column(String(4), nullable=False)
    quantity = Column(Numeric(18, 8), nullable=False)
    entry_price = Column(Numeric(18, 8), nullable=False)
    exit_price = Column(Numeric(18, 8), nullable=False)
    realized_pnl = Column(Numeric(12, 2), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=False)
    close_reason = Column(String(20), nullable=False)
    strategy_version = Column(String(20), nullable=False, default="1.0")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(50), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class AlertHistory(Base):
    __tablename__ = "alert_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(30), nullable=False)
    asset_symbol = Column(String(20))
    signal_id = Column(String(36), ForeignKey("signals.id"))
    message_hash = Column(String(64), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    telegram_message_id = Column(Integer)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(50), nullable=False)
    actor = Column(String(50), nullable=False)
    detail = Column(JSON)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SchedulerState(Base):
    __tablename__ = "scheduler_state"

    job_name = Column(String(50), primary_key=True)
    last_run_at = Column(DateTime(timezone=True))
    last_success_at = Column(DateTime(timezone=True))
    next_run_at = Column(DateTime(timezone=True))
    run_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class MarketDataMeta(Base):
    __tablename__ = "market_data_meta"

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    timeframe = Column(String(10), nullable=False)
    source = Column(String(20), nullable=False)
    candle_count = Column(Integer, nullable=False)
    valid_candle_count = Column(Integer)
    oldest_candle = Column(DateTime(timezone=True), nullable=False)
    newest_candle = Column(DateTime(timezone=True), nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    is_sufficient = Column(Boolean, nullable=False)
    validation_error = Column(Text)

    __table_args__ = (
        UniqueConstraint("asset_id", "timeframe", "source", name="uq_market_data_meta"),
    )


class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(Date, nullable=False, unique=True)
    balance_usd = Column(Numeric(12, 2), nullable=False)
    realized_pnl = Column(Numeric(12, 2), nullable=False)
    unrealized_pnl = Column(Numeric(12, 2), nullable=False)
    open_positions_count = Column(Integer, nullable=False, default=0)
    challenge_status = Column(String(10), nullable=False)
    peak_balance = Column(Numeric(12, 2), nullable=False)
    strategy_version = Column(String(20), nullable=False, default="1.0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
