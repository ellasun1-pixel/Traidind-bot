from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import settings
from src.database import get_session
from src.database.models import PaperPosition, TradeHistory, Asset, PortfolioSnapshot
from src.database.repository import PositionRepository, TradeHistoryRepository
from src.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class Position:
    def __init__(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        quantity: float,
        position_value_usd: float,
        commission_usd: float,
        spread_cost_usd: float,
        stop_loss: float = 0.0,
        risk_per_unit: float = 0.0,
        signal_id: Optional[int] = None,
    ):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.quantity = quantity
        self.position_value_usd = position_value_usd
        self.commission_usd = commission_usd
        self.spread_cost_usd = spread_cost_usd
        self.stop_loss = stop_loss
        self.risk_per_unit = risk_per_unit
        self.signal_id = signal_id
        self.status = "open"
        self.opened_at = datetime.now(timezone.utc)
        self.exit_price: Optional[float] = None
        self.realized_pnl: Optional[float] = None

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "BUY":
            return (current_price - self.entry_price) * self.quantity - self.commission_usd - self.spread_cost_usd
        return 0.0

    def close(self, exit_price: float) -> float:
        self.exit_price = exit_price
        exit_commission = self.position_value_usd * settings.commission_pct
        raw_pnl = (exit_price - self.entry_price) * self.quantity
        self.realized_pnl = raw_pnl - self.commission_usd - self.spread_cost_usd - exit_commission
        self.status = "closed"
        return self.realized_pnl

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "position_value_usd": self.position_value_usd,
            "stop_loss": self.stop_loss,
            "risk_per_unit": self.risk_per_unit,
            "status": self.status,
            "signal_id": self.signal_id,
        }


class PaperPortfolio:
    def __init__(self, starting_balance: float | None = None):
        self.starting_balance = starting_balance or settings.starting_balance
        self.balance_usd = self.starting_balance
        self.peak_balance = self.starting_balance
        self.realized_pnl_total = 0.0
        self.positions: list[Position] = []
        self.closed_trades: list[Position] = []
        self.risk_manager = RiskManager()
        self.challenge_status = "active"
        self._update_challenge_status()

    def confirm_buy(
        self,
        symbol: str,
        entry_price: float,
        position_value_usd: float,
        stop_loss: float,
        risk_dollars: float,
        signal_id: Optional[int] = None,
        prices: Optional[dict[str, float]] = None,
    ) -> tuple[bool, str]:
        if self.challenge_status != "active":
            return False, f"Challenge is {self.challenge_status} — no new trades"

        open_positions = [p for p in self.positions if p.status == "open"]
        total_open_risk = sum(
            abs(p.entry_price - p.stop_loss) * p.quantity
            for p in open_positions if p.stop_loss > 0
        )

        ok, reason = self.risk_manager.check_risk_budget(
            risk_dollars, total_open_risk, len(open_positions)
        )
        if not ok:
            return False, reason

        equity = self.get_total_equity(prices or {})
        adjusted_value, note = self.risk_manager.apply_circuit_breakers(
            equity, position_value_usd, "BUY"
        )
        if adjusted_value == 0:
            return False, note

        position_value_usd = adjusted_value
        commission = position_value_usd * settings.commission_pct
        spread_cost = position_value_usd * settings.spread_pct
        total_cost = position_value_usd + commission + spread_cost

        if total_cost > self.balance_usd:
            return False, "Insufficient balance"

        quantity = position_value_usd / entry_price
        risk_per_unit = abs(entry_price - stop_loss) if stop_loss > 0 else 0

        pos = Position(
            symbol=symbol,
            side="BUY",
            entry_price=entry_price,
            quantity=quantity,
            position_value_usd=position_value_usd,
            commission_usd=commission,
            spread_cost_usd=spread_cost,
            stop_loss=stop_loss,
            risk_per_unit=risk_per_unit,
            signal_id=signal_id,
        )
        self.positions.append(pos)
        self.balance_usd -= total_cost
        live_prices = dict(prices or {})
        live_prices[symbol] = entry_price
        self._update_challenge_status(live_prices)

        try:
            self._persist_buy(pos, signal_id)
        except Exception as e:
            logger.error("Failed to persist BUY to DB: %s", e)

        logger.info("BUY confirmed: %s %.4f @ $%.2f (value=$%.2f)", symbol, quantity, entry_price, position_value_usd)
        return True, f"Bought {quantity:.6f} {symbol} @ ${entry_price:.2f}"

    def confirm_sell(
        self, symbol: str, exit_price: float, signal_id: Optional[int] = None,
        prices: Optional[dict[str, float]] = None,
    ) -> tuple[bool, str]:
        open_pos = [p for p in self.positions if p.status == "open" and p.symbol == symbol]
        if not open_pos:
            return False, f"No open position for {symbol}"

        total_pnl = 0.0
        for pos in open_pos:
            pnl = pos.close(exit_price)
            total_pnl += pnl
            proceeds = exit_price * pos.quantity
            self.balance_usd += proceeds
            self.realized_pnl_total += pnl
            self.closed_trades.append(pos)

            try:
                self._persist_sell(pos, signal_id)
            except Exception as e:
                logger.error("Failed to persist SELL to DB: %s", e)

        self._update_challenge_status(prices or {})
        return True, f"Sold {symbol} @ ${exit_price:.2f}, P&L: ${total_pnl:.2f}"

    def get_open_positions(self) -> list[dict]:
        return [p.to_dict() for p in self.positions if p.status == "open"]

    def get_total_open_risk(self) -> float:
        return sum(
            abs(p.entry_price - p.stop_loss) * p.quantity
            for p in self.positions
            if p.status == "open" and p.stop_loss > 0
        )

    def get_unrealized_pnl(self, prices: dict[str, float]) -> float:
        total = 0.0
        for p in self.positions:
            if p.status == "open" and p.symbol in prices:
                total += p.unrealized_pnl(prices[p.symbol])
        return total

    def get_total_equity(self, prices: dict[str, float]) -> float:
        position_value = sum(
            prices.get(p.symbol, p.entry_price) * p.quantity
            for p in self.positions
            if p.status == "open"
        )
        return self.balance_usd + position_value

    def get_drawdown(self, prices: dict[str, float]) -> float:
        equity = self.get_total_equity(prices)
        if equity > self.peak_balance:
            self.peak_balance = equity
        if self.peak_balance == 0:
            return 0.0
        return (self.peak_balance - equity) / self.peak_balance

    def get_portfolio_summary(self, prices: dict[str, float]) -> dict:
        equity = self.get_total_equity(prices)
        unrealized = self.get_unrealized_pnl(prices)
        drawdown = self.get_drawdown(prices)
        open_positions = [p for p in self.positions if p.status == "open"]

        return {
            "balance_usd": round(self.balance_usd, 2),
            "total_equity": round(equity, 2),
            "unrealized_pnl": round(unrealized, 2),
            "realized_pnl": round(self.realized_pnl_total, 2),
            "drawdown_pct": round(drawdown * 100, 2),
            "peak_balance": round(self.peak_balance, 2),
            "distance_to_win": round(settings.win_level - equity, 2),
            "distance_to_loss": round(equity - settings.loss_level, 2),
            "challenge_status": self.challenge_status,
            "open_positions_count": len(open_positions),
            "open_positions": [p.to_dict() for p in open_positions],
            "total_trades": len(self.closed_trades),
        }

    @property
    def is_challenge_active(self) -> bool:
        return self.challenge_status == "active"

    def get_challenge_ended_message(self, prices: dict[str, float] | None = None) -> str:
        equity = self.get_total_equity(prices or {})
        if self.challenge_status == "won":
            return (
                "\U0001f3c6 *CHALLENGE WON!*\n\n"
                f"Final equity: ${equity:.2f}\n"
                f"Target was: ${settings.win_level:.2f}\n"
                f"Total trades: {len(self.closed_trades)}\n"
                f"Realized P&L: ${self.realized_pnl_total:.2f}\n\n"
                "The challenge has ended. No new signals will be generated.\n"
                "Use /new\\_challenge to start a fresh attempt."
            )
        else:
            return (
                "\U0001f6d1 *CHALLENGE LOST*\n\n"
                f"Final equity: ${equity:.2f}\n"
                f"Loss boundary: ${settings.loss_level:.2f}\n"
                f"Total trades: {len(self.closed_trades)}\n"
                f"Realized P&L: ${self.realized_pnl_total:.2f}\n\n"
                "The challenge has ended. No new signals will be generated.\n"
                "Use /new\\_challenge to start a fresh attempt."
            )

    def start_new_challenge(self, prices: dict[str, float] | None = None) -> tuple[dict, str]:
        """Archive current challenge and start fresh. Returns (archive, message)."""
        archive = {
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "outcome": self.challenge_status,
            "final_balance": round(self.balance_usd, 2),
            "final_equity": round(self.get_total_equity(prices or {}), 2),
            "peak_balance": round(self.peak_balance, 2),
            "realized_pnl": round(self.realized_pnl_total, 2),
            "total_trades": len(self.closed_trades),
            "trades": [
                {
                    "symbol": t.symbol,
                    "entry": t.entry_price,
                    "exit": t.exit_price,
                    "pnl": round(t.realized_pnl, 4) if t.realized_pnl else 0,
                    "opened": t.opened_at.isoformat() if t.opened_at else None,
                }
                for t in self.closed_trades
            ],
            "open_positions_abandoned": [p.to_dict() for p in self.positions if p.status == "open"],
        }

        self.balance_usd = self.starting_balance
        self.peak_balance = self.starting_balance
        self.realized_pnl_total = 0.0
        self.positions = []
        self.closed_trades = []
        self.challenge_status = "active"

        logger.warning(
            "NEW_CHALLENGE started. Previous outcome=%s final_equity=%.2f trades=%d",
            archive["outcome"], archive["final_equity"], archive["total_trades"],
        )
        msg = (
            f"\U0001f504 *New Paper Challenge started*\n\n"
            f"Previous outcome: {archive['outcome'].upper()}\n"
            f"Previous final equity: ${archive['final_equity']:.2f}\n"
            f"Previous trades: {archive['total_trades']}\n\n"
            f"Balance reset to ${self.starting_balance:.2f}. Good luck!"
        )
        return archive, msg

    def reset_challenge_status(self, prices: dict[str, float] | None = None) -> str:
        old = self.challenge_status
        equity = self.get_total_equity(prices or {})
        if old == "won":
            return f"Challenge already won (equity=${equity:.2f}) — cannot reset"
        if equity >= settings.win_level:
            self.challenge_status = "won"
        elif equity <= settings.loss_level:
            self.challenge_status = "lost"
        else:
            self.challenge_status = "active"
        logger.warning(
            "CHALLENGE_RESET %s→%s equity=%.2f", old, self.challenge_status, equity,
        )
        return f"Challenge status reset: {old} → {self.challenge_status} (equity=${equity:.2f})"

    def _persist_buy(self, pos: Position, signal_id: int | str | None) -> None:
        with get_session() as session:
            asset = session.query(Asset).filter(Asset.symbol == pos.symbol).first()
            if not asset:
                logger.warning("Asset %s not found in DB, skipping position persist", pos.symbol)
                return
            repo = PositionRepository(session)
            db_pos = repo.create(
                asset_id=asset.id,
                signal_id=str(signal_id) if signal_id else None,
                side="BUY",
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                stop_loss=pos.stop_loss,
            )
            pos._db_position_id = db_pos.id

    def _persist_sell(self, pos: Position, signal_id: int | str | None) -> None:
        with get_session() as session:
            asset = session.query(Asset).filter(Asset.symbol == pos.symbol).first()
            if not asset:
                return
            if hasattr(pos, "_db_position_id") and pos._db_position_id:
                db_pos = session.get(PaperPosition, pos._db_position_id)
                if db_pos:
                    repo = PositionRepository(session)
                    repo.close(db_pos, pos.exit_price, pos.realized_pnl or 0.0, "signal")
            else:
                open_db = (
                    session.query(PaperPosition)
                    .filter(PaperPosition.asset_id == asset.id, PaperPosition.is_open.is_(True))
                    .all()
                )
                repo = PositionRepository(session)
                for db_pos in open_db:
                    repo.close(db_pos, pos.exit_price, pos.realized_pnl or 0.0, "signal")

            trade_repo = TradeHistoryRepository(session)
            trade_repo.create(
                position_id=getattr(pos, "_db_position_id", None),
                asset_id=asset.id,
                signal_id=str(signal_id) if signal_id else None,
                side="BUY",
                quantity=pos.quantity,
                entry_price=pos.entry_price,
                exit_price=pos.exit_price,
                realized_pnl=round(pos.realized_pnl or 0.0, 2),
                entry_time=pos.opened_at,
                exit_time=datetime.now(timezone.utc),
                close_reason="signal",
            )

    @classmethod
    def restore_from_db(cls) -> "PaperPortfolio":
        portfolio = cls()
        try:
            with get_session() as session:
                open_positions = (
                    session.query(PaperPosition)
                    .join(Asset)
                    .filter(PaperPosition.is_open.is_(True))
                    .all()
                )

                closed_positions = (
                    session.query(PaperPosition)
                    .join(Asset)
                    .filter(PaperPosition.is_open.is_(False))
                    .order_by(PaperPosition.closed_at.asc())
                    .all()
                )

                latest_snap = (
                    session.query(PortfolioSnapshot)
                    .order_by(PortfolioSnapshot.created_at.desc())
                    .first()
                )

                if not open_positions and not closed_positions:
                    if latest_snap and float(latest_snap.cash_usd) != settings.starting_balance:
                        logger.info(
                            "DB has snapshot (equity=$%.2f) but no positions — "
                            "starting fresh at $%.2f",
                            float(latest_snap.equity_usd), settings.starting_balance,
                        )
                    return portfolio

                balance = settings.starting_balance
                realized_total = 0.0

                for cp in closed_positions:
                    entry_val = float(cp.entry_price) * float(cp.quantity)
                    commission = entry_val * settings.commission_pct
                    spread = entry_val * settings.spread_pct
                    cost = entry_val + commission + spread
                    balance -= cost
                    proceeds = float(cp.exit_price) * float(cp.quantity)
                    balance += proceeds
                    realized_total += float(cp.realized_pnl or 0)

                    closed_pos = Position(
                        symbol=cp.asset.symbol,
                        side=cp.side or "BUY",
                        entry_price=float(cp.entry_price),
                        quantity=float(cp.quantity),
                        position_value_usd=entry_val,
                        commission_usd=commission,
                        spread_cost_usd=spread,
                        stop_loss=float(cp.stop_loss or 0),
                    )
                    closed_pos.status = "closed"
                    closed_pos.exit_price = float(cp.exit_price)
                    closed_pos.realized_pnl = float(cp.realized_pnl or 0)
                    closed_pos.opened_at = cp.opened_at or datetime.now(timezone.utc)
                    closed_pos._db_position_id = cp.id
                    portfolio.closed_trades.append(closed_pos)

                for op in open_positions:
                    entry_val = float(op.entry_price) * float(op.quantity)
                    commission = entry_val * settings.commission_pct
                    spread = entry_val * settings.spread_pct
                    cost = entry_val + commission + spread
                    balance -= cost

                    open_pos = Position(
                        symbol=op.asset.symbol,
                        side=op.side or "BUY",
                        entry_price=float(op.entry_price),
                        quantity=float(op.quantity),
                        position_value_usd=entry_val,
                        commission_usd=commission,
                        spread_cost_usd=spread,
                        stop_loss=float(op.stop_loss or 0),
                        signal_id=op.signal_id,
                    )
                    open_pos._db_position_id = op.id
                    open_pos.opened_at = op.opened_at or datetime.now(timezone.utc)
                    portfolio.positions.append(open_pos)

                portfolio.balance_usd = round(balance, 2)
                portfolio.realized_pnl_total = round(realized_total, 2)

                entry_equity = portfolio.get_total_equity({})
                if latest_snap:
                    portfolio.peak_balance = max(
                        float(latest_snap.equity_usd),
                        entry_equity,
                        settings.starting_balance,
                    )
                else:
                    portfolio.peak_balance = max(
                        entry_equity,
                        settings.starting_balance,
                    )

                portfolio._update_challenge_status()

                logger.warning(
                    "PORTFOLIO_RESTORED from DB: balance=$%.2f realized_pnl=$%.2f "
                    "open_positions=%d closed_trades=%d peak=$%.2f status=%s",
                    portfolio.balance_usd, portfolio.realized_pnl_total,
                    len([p for p in portfolio.positions if p.status == "open"]),
                    len(portfolio.closed_trades),
                    portfolio.peak_balance, portfolio.challenge_status,
                )

        except Exception as e:
            logger.error("Failed to restore portfolio from DB, starting fresh: %s", e)
            return cls()

        return portfolio

    def _get_equity_estimate(self) -> float:
        position_value = sum(
            p.entry_price * p.quantity
            for p in self.positions
            if p.status == "open"
        )
        return self.balance_usd + position_value

    def _update_challenge_status(self, prices: dict[str, float] | None = None) -> str | None:
        """Returns a transition string ('won'/'lost') if challenge just ended, else None."""
        equity = self.get_total_equity(prices or {})
        old_status = self.challenge_status

        if equity >= settings.win_level:
            self.challenge_status = "won"
        elif equity <= settings.loss_level:
            self.challenge_status = "lost"
        elif self.challenge_status == "lost":
            self.challenge_status = "active"

        if self.challenge_status != old_status:
            logger.warning(
                "CHALLENGE_STATUS_CHANGE %s→%s balance=%.2f position_value=%.2f equity=%.2f "
                "win_level=%.2f loss_level=%.2f",
                old_status, self.challenge_status,
                self.balance_usd, equity - self.balance_usd, equity,
                settings.win_level, settings.loss_level,
            )
            if old_status == "active" and self.challenge_status in ("won", "lost"):
                return self.challenge_status
        else:
            logger.debug(
                "challenge_status=%s balance=%.2f equity=%.2f",
                self.challenge_status, self.balance_usd, equity,
            )
        return None
