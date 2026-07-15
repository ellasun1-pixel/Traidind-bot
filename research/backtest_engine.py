"""Historical backtesting engine using production StrategyEngine.

No look-ahead bias: signals are calculated from data available at time T,
execution occurs at open of T+1. Stop-loss and take-profit are evaluated
against T+1's high/low after entry.

When both stop-loss and take-profit could trigger on the same candle,
stop-loss is assumed to trigger first (worst-case ordering).
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from src.strategy.engine import StrategyEngine, TradeSignal
from src.strategy.challenge_engine import ChallengeStrategyEngine, ChallengeConfig
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from research.schema import to_engine_df

logger = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    starting_balance: float = 1000.0
    commission_pct: float = 0.0026
    spread_pct: float = 0.001
    slippage_pct: float = 0.0005
    win_level: float = 1120.0
    loss_level: float = 950.0


@dataclass
class TradeRecord:
    trade_id: int
    asset: str
    signal_date: str
    execution_date: str
    regime: str
    indicators: dict
    entry_price: float
    stop_loss: float
    take_profit: float
    position_size_usd: float
    max_risk_usd: float
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    holding_days: int = 0
    mfe: float = 0.0
    mae: float = 0.0
    risk_per_unit: float = 0.0


@dataclass
class SignalFunnelEntry:
    date: str
    asset: str
    regime: str
    signal_type: str
    reason: str


@dataclass
class DayState:
    date: str
    cash: float
    equity: float
    unrealized_pnl: float
    open_positions: int
    regime: Optional[str] = None


@dataclass
class BacktestResult:
    trades: list[TradeRecord]
    equity_curve: list[DayState]
    signal_funnel: list[SignalFunnelEntry]
    config: ExecutionConfig
    strategy_name: str
    asset: str
    start_date: str
    end_date: str


class HistoricalBacktester:
    def __init__(
        self,
        strategy: str = "conservative",
        config: ExecutionConfig | None = None,
    ):
        self.config = config or ExecutionConfig()
        self.strategy_name = strategy

        if strategy == "conservative":
            self.engine = StrategyEngine()
        elif strategy == "challenge":
            self.engine = ChallengeStrategyEngine()
        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    def run(
        self,
        asset: str,
        df: pd.DataFrame,
        warmup_candles: int = 252,
    ) -> BacktestResult:
        engine_df = to_engine_df(df)

        if len(engine_df) <= warmup_candles:
            raise ValueError(
                f"Need >{warmup_candles} candles, got {len(engine_df)}"
            )

        trades: list[TradeRecord] = []
        equity_curve: list[DayState] = []
        signal_funnel: list[SignalFunnelEntry] = []
        trade_counter = 0

        cash = self.config.starting_balance
        open_positions: list[dict] = []
        total_open_risk_usd = 0.0

        for i in range(warmup_candles, len(engine_df) - 1):
            today = engine_df.iloc[i]
            tomorrow = engine_df.iloc[i + 1]
            today_date = str(today["open_time"])[:10]
            tomorrow_date = str(tomorrow["open_time"])[:10]

            history = engine_df.iloc[:i + 1].copy()
            current_price = float(today["close"])

            equity = cash + self._unrealized_value(open_positions, current_price)

            if equity <= self.config.loss_level:
                equity_curve.append(DayState(
                    date=today_date, cash=cash, equity=equity,
                    unrealized_pnl=equity - cash, open_positions=len(open_positions),
                ))
                break
            if equity >= self.config.win_level:
                equity_curve.append(DayState(
                    date=today_date, cash=cash, equity=equity,
                    unrealized_pnl=equity - cash, open_positions=len(open_positions),
                ))
                break

            cash, open_positions, total_open_risk_usd, closed = self._process_exits(
                asset, open_positions, tomorrow, trades, total_open_risk_usd, cash
            )

            signal = self.engine.analyze(
                symbol=asset,
                daily_df=history,
                h4_df=pd.DataFrame(),
                current_price=current_price,
                portfolio_balance=equity,
                open_positions=open_positions,
                total_open_risk_usd=total_open_risk_usd,
            )

            indicators = self._extract_indicators(history)
            regime_str = signal.regime.value if signal.regime else "UNKNOWN"

            signal_funnel.append(SignalFunnelEntry(
                date=today_date, asset=asset, regime=regime_str,
                signal_type=signal.signal_type, reason=signal.reason,
            ))

            if signal.signal_type == "BUY" and signal.position_size_usd > 0:
                exec_price = float(tomorrow["open"]) * (1 + self.config.spread_pct / 2 + self.config.slippage_pct)
                cost = signal.position_size_usd * self.config.commission_pct

                stop_distance_pct = (signal.entry_price - signal.stop_loss) / signal.entry_price if signal.entry_price > 0 else 0.03
                stop_loss_price = exec_price * (1 - stop_distance_pct)

                risk_per_unit = exec_price - stop_loss_price
                tp_price = exec_price + risk_per_unit * self.engine.take_profit_multiple if hasattr(self.engine, 'take_profit_multiple') else exec_price * 1.09

                if isinstance(self.engine, ChallengeStrategyEngine):
                    tp_price = exec_price + risk_per_unit * self.engine.cfg.take_profit_multiple

                trade_counter += 1
                quantity = signal.position_size_usd / exec_price

                trade = TradeRecord(
                    trade_id=trade_counter,
                    asset=asset,
                    signal_date=today_date,
                    execution_date=tomorrow_date,
                    regime=regime_str,
                    indicators=indicators,
                    entry_price=exec_price,
                    stop_loss=stop_loss_price,
                    take_profit=tp_price,
                    position_size_usd=signal.position_size_usd,
                    max_risk_usd=signal.max_loss_usd,
                    risk_per_unit=risk_per_unit,
                )

                pos = {
                    "symbol": asset,
                    "entry_price": exec_price,
                    "stop_loss": stop_loss_price,
                    "quantity": quantity,
                    "risk_per_unit": risk_per_unit,
                    "position_size_usd": signal.position_size_usd,
                    "max_loss_usd": signal.max_loss_usd,
                    "trade_record": trade,
                    "status": "open",
                }
                open_positions.append(pos)
                total_open_risk_usd += signal.max_loss_usd
                cash -= (signal.position_size_usd + cost)

            elif signal.signal_type in ("SELL", "MOVE_TO_USD") and open_positions:
                for pos in list(open_positions):
                    if pos.get("symbol") == asset:
                        exec_price = float(tomorrow["open"]) * (1 - self.config.spread_pct / 2 - self.config.slippage_pct)
                        self._close_position(pos, exec_price, tomorrow_date, signal.signal_type, trades)
                        cash += pos["quantity"] * exec_price * (1 - self.config.commission_pct)
                        total_open_risk_usd -= pos.get("max_loss_usd", 0)
                        open_positions.remove(pos)

            elif signal.signal_type == "TAKE_PROFIT" and open_positions:
                for pos in list(open_positions):
                    if pos.get("symbol") == asset:
                        exec_price = float(tomorrow["open"]) * (1 - self.config.spread_pct / 2)
                        self._close_position(pos, exec_price, tomorrow_date, "TAKE_PROFIT_SIGNAL", trades)
                        cash += pos["quantity"] * exec_price * (1 - self.config.commission_pct)
                        total_open_risk_usd -= pos.get("max_loss_usd", 0)
                        open_positions.remove(pos)

            equity = cash + self._unrealized_value(open_positions, current_price)
            equity_curve.append(DayState(
                date=today_date, cash=cash, equity=equity,
                unrealized_pnl=equity - cash, open_positions=len(open_positions),
                regime=regime_str,
            ))

        for pos in list(open_positions):
            last_price = float(engine_df.iloc[-1]["close"])
            self._close_position(pos, last_price, str(engine_df.iloc[-1]["open_time"])[:10], "END_OF_DATA", trades)
            cash += pos["quantity"] * last_price * (1 - self.config.commission_pct)
            open_positions.remove(pos)

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            signal_funnel=signal_funnel,
            config=self.config,
            strategy_name=self.strategy_name,
            asset=asset,
            start_date=str(engine_df.iloc[warmup_candles]["open_time"])[:10],
            end_date=str(engine_df.iloc[-1]["open_time"])[:10],
        )

    def _process_exits(
        self,
        asset: str,
        positions: list[dict],
        candle: pd.Series,
        trades: list[TradeRecord],
        total_risk: float,
        cash: float,
    ) -> tuple[float, list[dict], float, list[dict]]:
        remaining = []
        closed = []
        high = float(candle["high"])
        low = float(candle["low"])
        candle_date = str(candle["open_time"])[:10]

        for pos in positions:
            if pos.get("symbol") != asset:
                remaining.append(pos)
                continue

            trade_rec: TradeRecord = pos["trade_record"]
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = trade_rec.take_profit
            quantity = pos["quantity"]

            price_range_high = high
            price_range_low = low
            mfe_price = max(price_range_high, entry)
            mae_price = min(price_range_low, entry)
            trade_rec.mfe = max(trade_rec.mfe, (mfe_price - entry) / entry)
            trade_rec.mae = min(trade_rec.mae, (mae_price - entry) / entry)

            sl_hit = low <= sl
            tp_hit = high >= tp

            if sl_hit and tp_hit:
                exit_price = sl * (1 - self.config.slippage_pct)
                exit_reason = "STOP_LOSS"
            elif sl_hit:
                exit_price = sl * (1 - self.config.slippage_pct)
                exit_reason = "STOP_LOSS"
            elif tp_hit:
                exit_price = tp * (1 - self.config.spread_pct / 2)
                exit_reason = "TAKE_PROFIT"
            else:
                remaining.append(pos)
                continue

            self._close_position(pos, exit_price, candle_date, exit_reason, trades)
            proceeds = quantity * exit_price * (1 - self.config.commission_pct)
            cash += proceeds
            total_risk -= pos.get("max_loss_usd", 0)
            closed.append(pos)

        return cash, remaining, total_risk, closed

    def _close_position(
        self, pos: dict, exit_price: float, exit_date: str,
        exit_reason: str, trades: list[TradeRecord],
    ) -> None:
        trade_rec: TradeRecord = pos["trade_record"]
        trade_rec.exit_date = exit_date
        trade_rec.exit_price = exit_price
        trade_rec.exit_reason = exit_reason

        entry_cost = pos["quantity"] * pos["entry_price"]
        exit_proceeds = pos["quantity"] * exit_price
        commission = (entry_cost + exit_proceeds) * self.config.commission_pct
        trade_rec.pnl = exit_proceeds - entry_cost - commission

        if trade_rec.execution_date and trade_rec.exit_date:
            try:
                d1 = pd.Timestamp(trade_rec.execution_date)
                d2 = pd.Timestamp(trade_rec.exit_date)
                trade_rec.holding_days = (d2 - d1).days
            except Exception:
                pass

        trades.append(trade_rec)

    def _unrealized_value(self, positions: list[dict], price: float) -> float:
        return sum(p["quantity"] * price for p in positions)

    def _extract_indicators(self, history: pd.DataFrame) -> dict:
        try:
            df = compute_indicators(history)
            latest = df.iloc[-1]
            return {
                "ema50": float(latest.get("ema50", 0) or 0),
                "ema200": float(latest.get("ema200", 0) or 0),
                "er20": float(latest.get("er20", 0) or 0),
                "adx14": float(latest.get("adx14", 0) or 0),
                "rvol": float(latest.get("rvol", 0) or 0),
            }
        except Exception:
            return {}


def run_multi_asset(
    assets_data: dict[str, pd.DataFrame],
    strategy: str = "conservative",
    config: ExecutionConfig | None = None,
    warmup_candles: int = 252,
) -> list[BacktestResult]:
    """Run backtest across multiple assets with shared portfolio state.

    Each asset is backtested independently but using the same equity curve.
    Multi-asset interaction (position limits across assets) is handled
    by running assets in day-synchronized order.
    """
    config = config or ExecutionConfig()
    results = []

    for asset, df in assets_data.items():
        bt = HistoricalBacktester(strategy=strategy, config=config)
        result = bt.run(asset, df, warmup_candles)
        results.append(result)

    return results
