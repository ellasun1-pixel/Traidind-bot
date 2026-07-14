from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import (
    create_engine, Column, Integer, Float, Text, Boolean, DateTime, JSON,
    ForeignKey, UniqueConstraint, func,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

from src.config import settings

db_url = settings.database_url
if db_url.startswith("sqlite:///"):
    db_path = db_url.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(db_url, echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

Base = declarative_base()


class Asset(Base):
    __tablename__ = "assets"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(Text, nullable=False, unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class PriceHistory(Base):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    source = Column(Text, nullable=False)
    timeframe = Column(Text, nullable=False)
    open_time = Column(DateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float)
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("asset_id", "source", "timeframe", "open_time"),
    )


class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    signal_type = Column(Text, nullable=False)
    priority = Column(Text, nullable=False)
    regime = Column(Text, nullable=False)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    position_size_usd = Column(Float)
    max_loss_usd = Column(Float)
    reason = Column(Text)
    signal_data = Column(JSON)
    status = Column(Text, default="pending")
    created_at = Column(DateTime, server_default=func.now())
    confirmed_at = Column(DateTime)
    notified_at = Column(DateTime)


class VirtualTrade(Base):
    __tablename__ = "virtual_trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"))
    asset_id = Column(Integer, ForeignKey("assets.id"), nullable=False)
    side = Column(Text, nullable=False)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    position_value_usd = Column(Float, nullable=False)
    commission_usd = Column(Float, default=0)
    spread_cost_usd = Column(Float, default=0)
    exit_price = Column(Float)
    exit_at = Column(DateTime)
    realized_pnl = Column(Float)
    status = Column(Text, default="open")
    created_at = Column(DateTime, server_default=func.now())


class PortfolioState(Base):
    __tablename__ = "portfolio_state"
    id = Column(Integer, primary_key=True, autoincrement=True)
    balance_usd = Column(Float, nullable=False)
    total_equity = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, default=0)
    realized_pnl = Column(Float, default=0)
    drawdown_pct = Column(Float, default=0)
    peak_balance = Column(Float, nullable=False)
    distance_to_win = Column(Float)
    distance_to_loss = Column(Float)
    challenge_status = Column(Text, default="active")
    open_positions_count = Column(Integer, default=0)
    snapshot_at = Column(DateTime, server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"
    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(Text, nullable=False)
    details = Column(JSON)
    created_at = Column(DateTime, server_default=func.now())


def init_db():
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
