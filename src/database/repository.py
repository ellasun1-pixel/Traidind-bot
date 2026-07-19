from __future__ import annotations

import logging
from datetime import datetime, timezone, date, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import and_, update

from src.database.models import (
    Asset, Signal, PaperAccount, PaperPosition,
    TradeHistory, AppSetting, AuditLog, AlertHistory,
    SchedulerState, MarketDataMeta, DailySnapshot,
    PriceHistory, HealthTransition, PortfolioSnapshot,
)

logger = logging.getLogger(__name__)


class AssetRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_all_enabled(self) -> list[Asset]:
        return self.session.query(Asset).filter(Asset.enabled.is_(True)).all()

    def get_by_symbol(self, symbol: str) -> Optional[Asset]:
        return self.session.query(Asset).filter(Asset.symbol == symbol).first()

    def get_by_id(self, asset_id: int) -> Optional[Asset]:
        return self.session.query(Asset).filter(Asset.id == asset_id).first()

    def upsert(self, symbol: str, kraken_pair: str = None,
               coinbase_pair: str = None, **kwargs) -> Asset:
        asset = self.get_by_symbol(symbol)
        if asset is None:
            asset = Asset(symbol=symbol, kraken_pair=kraken_pair,
                          coinbase_pair=coinbase_pair, **kwargs)
            self.session.add(asset)
        else:
            if kraken_pair is not None:
                asset.kraken_pair = kraken_pair
            if coinbase_pair is not None:
                asset.coinbase_pair = coinbase_pair
            for k, v in kwargs.items():
                if hasattr(asset, k):
                    setattr(asset, k, v)
            asset.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return asset


class PaperAccountRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_or_create(self, starting_balance: float = 1000.0) -> PaperAccount:
        account = self.session.query(PaperAccount).first()
        if account is None:
            account = PaperAccount(
                balance_usd=starting_balance,
                peak_balance=starting_balance,
                starting_balance=starting_balance,
            )
            self.session.add(account)
            self.session.flush()
        return account

    def update_balance(self, account: PaperAccount, new_balance: float):
        account.balance_usd = new_balance
        if new_balance > float(account.peak_balance):
            account.peak_balance = new_balance
        account.updated_at = datetime.now(timezone.utc)
        self.session.flush()

    def reset_daily_loss(self, account: PaperAccount):
        today = datetime.now(timezone.utc).date()
        if account.daily_loss_date != today:
            account.daily_loss = 0.0
            account.daily_loss_date = today
            self.session.flush()


class SignalRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **kwargs) -> Signal:
        signal = Signal(**kwargs)
        self.session.add(signal)
        self.session.flush()
        return signal

    def get_by_id(self, signal_id: str) -> Optional[Signal]:
        return self.session.query(Signal).filter(Signal.id == signal_id).first()

    def get_pending(self, asset_id: int = None) -> list[Signal]:
        query = self.session.query(Signal).filter(Signal.status == "pending")
        if asset_id is not None:
            query = query.filter(Signal.asset_id == asset_id)
        return query.all()

    def get_pending_not_expired(self) -> list[Signal]:
        now = datetime.now(timezone.utc)
        return (
            self.session.query(Signal)
            .filter(and_(Signal.status == "pending", Signal.expires_at > now))
            .all()
        )

    def expire_old_signals(self) -> int:
        now = datetime.now(timezone.utc)
        count = (
            self.session.query(Signal)
            .filter(and_(Signal.status == "pending", Signal.expires_at <= now))
            .update({"status": "expired"}, synchronize_session="fetch")
        )
        self.session.flush()
        return count

    def confirm(self, signal: Signal) -> Signal:
        signal.status = "confirmed"
        signal.confirmed_at = datetime.now(timezone.utc)
        self.session.flush()
        return signal

    def reject(self, signal: Signal) -> Signal:
        signal.status = "rejected"
        signal.rejected_at = datetime.now(timezone.utc)
        self.session.flush()
        return signal

    def mark_executed(self, signal: Signal) -> Signal:
        signal.status = "executed_paper"
        signal.executed_at = datetime.now(timezone.utc)
        self.session.flush()
        return signal


class PositionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **kwargs) -> PaperPosition:
        position = PaperPosition(**kwargs)
        self.session.add(position)
        self.session.flush()
        return position

    def get_open(self, asset_id: int = None) -> list[PaperPosition]:
        query = self.session.query(PaperPosition).filter(PaperPosition.is_open.is_(True))
        if asset_id is not None:
            query = query.filter(PaperPosition.asset_id == asset_id)
        return query.all()

    def get_open_count(self) -> int:
        return self.session.query(PaperPosition).filter(PaperPosition.is_open.is_(True)).count()

    def close(self, position: PaperPosition, exit_price: float,
              realized_pnl: float, close_reason: str) -> PaperPosition:
        position.is_open = False
        position.exit_price = exit_price
        position.realized_pnl = realized_pnl
        position.close_reason = close_reason
        position.closed_at = datetime.now(timezone.utc)
        self.session.flush()
        return position


class TradeHistoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, **kwargs) -> TradeHistory:
        trade = TradeHistory(**kwargs)
        self.session.add(trade)
        self.session.flush()
        return trade

    def get_recent(self, limit: int = 20) -> list[TradeHistory]:
        return (
            self.session.query(TradeHistory)
            .order_by(TradeHistory.exit_time.desc())
            .limit(limit)
            .all()
        )


class AuditLogRepository:
    def __init__(self, session: Session):
        self.session = session

    def log(self, action: str, actor: str, detail: dict = None) -> AuditLog:
        entry = AuditLog(action=action, actor=actor, detail=detail)
        self.session.add(entry)
        self.session.flush()
        return entry


class AlertHistoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def is_duplicate(self, message_hash: str) -> bool:
        return (
            self.session.query(AlertHistory)
            .filter(AlertHistory.message_hash == message_hash)
            .first()
        ) is not None

    def record(self, alert_type: str, message_hash: str,
               asset_symbol: str = None, signal_id: str = None,
               telegram_message_id: int = None) -> AlertHistory:
        entry = AlertHistory(
            alert_type=alert_type,
            asset_symbol=asset_symbol,
            signal_id=signal_id,
            message_hash=message_hash,
            telegram_message_id=telegram_message_id,
        )
        self.session.add(entry)
        self.session.flush()
        return entry


class SchedulerStateRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_or_create(self, job_name: str) -> SchedulerState:
        state = self.session.query(SchedulerState).filter(
            SchedulerState.job_name == job_name
        ).first()
        if state is None:
            state = SchedulerState(job_name=job_name)
            self.session.add(state)
            self.session.flush()
        return state

    def get_all(self) -> list[SchedulerState]:
        return self.session.query(SchedulerState).all()

    def try_acquire_lock(
        self, job_name: str, lock_owner: str, lock_duration_seconds: int = 300
    ) -> bool:
        now = datetime.now(timezone.utc)
        self.get_or_create(job_name)
        self.session.flush()

        new_expires = now + timedelta(seconds=lock_duration_seconds)

        # Atomic conditional UPDATE: only acquire if lock is free or expired.
        # On PostgreSQL this is a single atomic statement; on SQLite it is
        # safe because SQLite serializes writes.
        from sqlalchemy import or_
        rows = (
            self.session.query(SchedulerState)
            .filter(
                SchedulerState.job_name == job_name,
                or_(
                    SchedulerState.lock_owner.is_(None),
                    SchedulerState.lock_expires_at.is_(None),
                    SchedulerState.lock_expires_at <= now,
                ),
            )
            .update(
                {
                    SchedulerState.lock_owner: lock_owner,
                    SchedulerState.lock_expires_at: new_expires,
                    SchedulerState.current_status: "running",
                    SchedulerState.last_started_at: now,
                    SchedulerState.last_run_at: now,
                    SchedulerState.run_count: SchedulerState.run_count + 1,
                },
                synchronize_session="fetch",
            )
        )
        self.session.flush()
        return rows > 0

    def release_lock(self, job_name: str) -> None:
        state = self.get_or_create(job_name)
        state.lock_owner = None
        state.lock_expires_at = None
        self.session.flush()

    def mark_started(self, job_name: str) -> SchedulerState:
        state = self.get_or_create(job_name)
        state.last_run_at = datetime.now(timezone.utc)
        state.last_started_at = state.last_run_at
        state.current_status = "running"
        state.run_count += 1
        self.session.flush()
        return state

    def mark_success(self, job_name: str, duration_ms: int = None,
                     next_run_at: datetime = None) -> SchedulerState:
        now = datetime.now(timezone.utc)
        state = self.get_or_create(job_name)
        state.last_success_at = now
        state.last_completed_at = now
        state.current_status = "idle"
        state.last_error = None
        state.success_count = (state.success_count or 0) + 1
        if duration_ms is not None:
            state.last_duration_ms = duration_ms
        if next_run_at:
            state.next_run_at = next_run_at
        state.lock_owner = None
        state.lock_expires_at = None
        self.session.flush()
        return state

    def mark_failure(self, job_name: str, error: str,
                     duration_ms: int = None) -> SchedulerState:
        now = datetime.now(timezone.utc)
        state = self.get_or_create(job_name)
        state.last_error = error
        state.last_completed_at = now
        state.current_status = "idle"
        state.failure_count = (state.failure_count or 0) + 1
        if duration_ms is not None:
            state.last_duration_ms = duration_ms
        state.lock_owner = None
        state.lock_expires_at = None
        self.session.flush()
        return state


class AppSettingRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, key: str, default: str = None) -> Optional[str]:
        setting = self.session.query(AppSetting).filter(AppSetting.key == key).first()
        if setting is None:
            return default
        return setting.value

    def set(self, key: str, value: str):
        setting = self.session.query(AppSetting).filter(AppSetting.key == key).first()
        if setting is None:
            setting = AppSetting(key=key, value=value)
            self.session.add(setting)
        else:
            setting.value = value
            setting.updated_at = datetime.now(timezone.utc)
        self.session.flush()


class DailySnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def save_snapshot(self, snapshot_date: date, balance_usd: float,
                      realized_pnl: float, unrealized_pnl: float,
                      open_positions_count: int, challenge_status: str,
                      peak_balance: float) -> DailySnapshot:
        existing = self.session.query(DailySnapshot).filter(
            DailySnapshot.snapshot_date == snapshot_date
        ).first()
        if existing:
            existing.balance_usd = balance_usd
            existing.realized_pnl = realized_pnl
            existing.unrealized_pnl = unrealized_pnl
            existing.open_positions_count = open_positions_count
            existing.challenge_status = challenge_status
            existing.peak_balance = peak_balance
            self.session.flush()
            return existing

        snapshot = DailySnapshot(
            snapshot_date=snapshot_date,
            balance_usd=balance_usd,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_positions_count=open_positions_count,
            challenge_status=challenge_status,
            peak_balance=peak_balance,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot


class PriceHistoryRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_candle(self, asset_id: int, timeframe: str, open_time: datetime,
                      open_: float, high: float, low: float, close: float,
                      volume: float, source: str) -> PriceHistory:
        existing = (
            self.session.query(PriceHistory)
            .filter(and_(
                PriceHistory.asset_id == asset_id,
                PriceHistory.timeframe == timeframe,
                PriceHistory.open_time == open_time,
            ))
            .first()
        )
        if existing:
            existing.open = open_
            existing.high = high
            existing.low = low
            existing.close = close
            existing.volume = volume
            existing.source = source
            existing.fetched_at = datetime.now(timezone.utc)
            self.session.flush()
            return existing

        record = PriceHistory(
            asset_id=asset_id, timeframe=timeframe, open_time=open_time,
            open=open_, high=high, low=low, close=close,
            volume=volume, source=source,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def bulk_upsert(self, asset_id: int, timeframe: str, source: str,
                    candles: list) -> int:
        count = 0
        for c in candles:
            self.upsert_candle(
                asset_id=asset_id, timeframe=timeframe,
                open_time=c.open_time, open_=c.open, high=c.high,
                low=c.low, close=c.close, volume=c.volume, source=source,
            )
            count += 1
        return count


class MarketDataMetaRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, asset_id: int, timeframe: str, source: str,
               candle_count: int, valid_candle_count: int,
               oldest_candle: datetime, newest_candle: datetime,
               is_sufficient: bool, validation_error: str = None) -> MarketDataMeta:
        existing = (
            self.session.query(MarketDataMeta)
            .filter(and_(
                MarketDataMeta.asset_id == asset_id,
                MarketDataMeta.timeframe == timeframe,
                MarketDataMeta.source == source,
            ))
            .first()
        )
        if existing:
            existing.candle_count = candle_count
            existing.valid_candle_count = valid_candle_count
            existing.oldest_candle = oldest_candle
            existing.newest_candle = newest_candle
            existing.is_sufficient = is_sufficient
            existing.validation_error = validation_error
            existing.fetched_at = datetime.now(timezone.utc)
            self.session.flush()
            return existing

        meta = MarketDataMeta(
            asset_id=asset_id, timeframe=timeframe, source=source,
            candle_count=candle_count, valid_candle_count=valid_candle_count,
            oldest_candle=oldest_candle, newest_candle=newest_candle,
            is_sufficient=is_sufficient, validation_error=validation_error,
        )
        self.session.add(meta)
        self.session.flush()
        return meta


class HealthTransitionRepository:
    def __init__(self, session: Session):
        self.session = session

    def record(self, component: str, old_status: str, new_status: str,
               reason: str = None) -> HealthTransition:
        entry = HealthTransition(
            component=component,
            old_status=old_status,
            new_status=new_status,
            reason=reason,
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def get_latest_for_component(self, component: str) -> Optional[HealthTransition]:
        return (
            self.session.query(HealthTransition)
            .filter(HealthTransition.component == component)
            .order_by(HealthTransition.created_at.desc())
            .first()
        )

    def mark_recovery(self, component: str) -> Optional[HealthTransition]:
        latest = self.get_latest_for_component(component)
        if latest and latest.recovered_at is None and latest.new_status != "HEALTHY":
            now = datetime.now(timezone.utc)
            latest.recovered_at = now
            created = latest.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            latest.recovery_seconds = int((now - created).total_seconds())
            self.session.flush()
            return latest
        return None

    def get_recent(self, limit: int = 50) -> list[HealthTransition]:
        return (
            self.session.query(HealthTransition)
            .order_by(HealthTransition.created_at.desc())
            .limit(limit)
            .all()
        )


class PortfolioSnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def record(
        self,
        trigger: str,
        cash_usd: float,
        equity_usd: float,
        realized_pnl: float,
        open_positions_count: int,
        open_positions_summary: list[dict] | None,
        challenge_status: str,
    ) -> PortfolioSnapshot:
        snap = PortfolioSnapshot(
            trigger=trigger,
            cash_usd=round(cash_usd, 2),
            equity_usd=round(equity_usd, 2),
            realized_pnl=round(realized_pnl, 2),
            open_positions_count=open_positions_count,
            open_positions_summary=open_positions_summary,
            challenge_status=challenge_status,
        )
        self.session.add(snap)
        self.session.flush()
        return snap
