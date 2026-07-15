"""
Backtesting framework — runs a strategy engine over historical daily OHLCV data,
simulating the paper trading lifecycle: signals → fills → stops → take-profits.

Produces a BacktestResult with trade count, win rate, average return,
maximum drawdown, and estimated probability of passing the Kraken Challenge.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import pandas as pd

from src.strategy.indicators import compute_indicators

logger = logging.getLogger(__name__)

WIN_LEVEL = 1120.0
LOSS_LEVEL = 950.0
STARTING_BALANCE = 1000.0


class StrategyLike(Protocol):
    def analyze(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        h4_df: pd.DataFrame,
        current_price: float,
        portfolio_balance: float,
        open_positions: list[dict],
        total_open_risk_usd: float,
    ): ...


@dataclass
class ClosedTrade:
    symbol: str
    entry_price: float
    exit_price: float
    position_value: float
    risk_dollars: float
    stop_loss: float
    entry_day: int
    exit_day: int
    exit_reason: str

    @property
    def pnl(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return self.position_value * (self.exit_price - self.entry_price) / self.entry_price

    @property
    def return_pct(self) -> float:
        if self.position_value <= 0:
            return 0.0
        return self.pnl / self.position_value


@dataclass
class BacktestResult:
    strategy_name: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_return_pct: float
    total_pnl: float
    max_drawdown_pct: float
    final_balance: float
    peak_balance: float
    trough_balance: float
    challenge_passed: bool
    challenge_failed: bool
    days_simulated: int
    trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def run_backtest(
    engine: StrategyLike,
    symbol: str,
    daily_df: pd.DataFrame,
    strategy_name: str = "Strategy",
    starting_balance: float = STARTING_BALANCE,
    commission_pct: float = 0.0026,
    spread_pct: float = 0.001,
) -> BacktestResult:
    if len(daily_df) < 210:
        return BacktestResult(
            strategy_name=strategy_name, total_trades=0, wins=0, losses=0,
            win_rate=0.0, avg_return_pct=0.0, total_pnl=0.0, max_drawdown_pct=0.0,
            final_balance=starting_balance, peak_balance=starting_balance,
            trough_balance=starting_balance, challenge_passed=False,
            challenge_failed=False, days_simulated=0,
        )

    enriched = compute_indicators(daily_df)

    balance = starting_balance
    peak = balance
    trough = balance
    max_dd = 0.0
    open_positions: list[dict] = []
    closed_trades: list[ClosedTrade] = []
    equity_curve = [balance]
    passed = False
    failed = False

    lookback = 200

    for day_idx in range(lookback, len(enriched)):
        window = enriched.iloc[max(0, day_idx - 299):day_idx + 1]
        today = enriched.iloc[day_idx]
        current_price = float(today["close"])
        high = float(today.get("high", current_price))
        low = float(today.get("low", current_price))

        still_open = []
        for pos in open_positions:
            stop = pos["stop_loss"]
            tp = pos.get("take_profit", 0)

            if low <= stop:
                exit_price = stop
                cost = pos["position_value"] * (commission_pct + spread_pct)
                pnl = pos["position_value"] * (exit_price - pos["entry_price"]) / pos["entry_price"] - cost
                balance += pnl
                closed_trades.append(ClosedTrade(
                    symbol=symbol, entry_price=pos["entry_price"],
                    exit_price=exit_price, position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"], stop_loss=stop,
                    entry_day=pos["entry_day"], exit_day=day_idx,
                    exit_reason="stop_loss",
                ))
                continue

            if tp and high >= tp:
                exit_price = tp
                cost = pos["position_value"] * (commission_pct + spread_pct)
                pnl = pos["position_value"] * (exit_price - pos["entry_price"]) / pos["entry_price"] - cost
                balance += pnl
                closed_trades.append(ClosedTrade(
                    symbol=symbol, entry_price=pos["entry_price"],
                    exit_price=exit_price, position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"], stop_loss=stop,
                    entry_day=pos["entry_day"], exit_day=day_idx,
                    exit_reason="take_profit",
                ))
                continue

            still_open.append(pos)

        open_positions = still_open

        if balance <= LOSS_LEVEL:
            failed = True
            break
        if balance >= WIN_LEVEL:
            passed = True
            break

        total_risk = sum(p["risk_dollars"] for p in open_positions)
        pos_dicts = [
            {"symbol": p["symbol"], "stop_loss": p["stop_loss"],
             "entry_price": p["entry_price"], "risk_per_unit": p.get("risk_per_unit", 0),
             "status": "open"}
            for p in open_positions
        ]

        signal = engine.analyze(
            symbol, window, window, current_price,
            balance, pos_dicts, total_risk,
        )

        if signal.signal_type == "BUY" and signal.position_size_usd > 0:
            slippage_pct = commission_pct + spread_pct
            effective_entry = current_price * (1 + slippage_pct)
            stop_distance = current_price - signal.stop_loss
            risk_per_unit = stop_distance if stop_distance > 0 else current_price * 0.03
            tp_price = 0.0
            if hasattr(engine, 'cfg'):
                tp_price = current_price + risk_per_unit * engine.cfg.take_profit_multiple
            elif hasattr(engine, 'take_profit_multiple'):
                tp_price = current_price + risk_per_unit * engine.take_profit_multiple

            open_positions.append({
                "symbol": symbol,
                "entry_price": effective_entry,
                "stop_loss": signal.stop_loss,
                "take_profit": tp_price,
                "position_value": signal.position_size_usd,
                "risk_dollars": signal.max_loss_usd,
                "risk_per_unit": risk_per_unit,
                "entry_day": day_idx,
            })

        elif signal.signal_type == "SELL" and open_positions:
            for pos in open_positions:
                cost = pos["position_value"] * (commission_pct + spread_pct)
                pnl = pos["position_value"] * (current_price - pos["entry_price"]) / pos["entry_price"] - cost
                balance += pnl
                closed_trades.append(ClosedTrade(
                    symbol=symbol, entry_price=pos["entry_price"],
                    exit_price=current_price, position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"], stop_loss=pos["stop_loss"],
                    entry_day=pos["entry_day"], exit_day=day_idx,
                    exit_reason="signal_sell",
                ))
            open_positions = []

        peak = max(peak, balance)
        trough = min(trough, balance)
        if peak > 0:
            dd = (peak - balance) / peak
            max_dd = max(max_dd, dd)

        equity_curve.append(balance)

    for pos in open_positions:
        final_price = float(enriched.iloc[-1]["close"])
        cost = pos["position_value"] * (commission_pct + spread_pct)
        pnl = pos["position_value"] * (final_price - pos["entry_price"]) / pos["entry_price"] - cost
        balance += pnl
        closed_trades.append(ClosedTrade(
            symbol=symbol, entry_price=pos["entry_price"],
            exit_price=final_price, position_value=pos["position_value"],
            risk_dollars=pos["risk_dollars"], stop_loss=pos["stop_loss"],
            entry_day=pos["entry_day"], exit_day=len(enriched) - 1,
            exit_reason="end_of_data",
        ))

    wins = sum(1 for t in closed_trades if t.pnl > 0)
    losses = sum(1 for t in closed_trades if t.pnl <= 0)
    total = len(closed_trades)
    avg_ret = np.mean([t.return_pct for t in closed_trades]) if closed_trades else 0.0

    return BacktestResult(
        strategy_name=strategy_name,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=wins / total if total > 0 else 0.0,
        avg_return_pct=float(avg_ret),
        total_pnl=balance - starting_balance,
        max_drawdown_pct=max_dd,
        final_balance=balance,
        peak_balance=peak,
        trough_balance=trough,
        challenge_passed=passed,
        challenge_failed=failed,
        days_simulated=len(equity_curve),
        trades=closed_trades,
        equity_curve=equity_curve,
    )


def run_monte_carlo(
    engine: StrategyLike,
    symbol: str,
    daily_df: pd.DataFrame,
    strategy_name: str = "Strategy",
    n_simulations: int = 100,
    window_days: int = 90,
) -> dict:
    if len(daily_df) < window_days + 210:
        return {
            "strategy_name": strategy_name,
            "n_simulations": 0,
            "pass_rate": 0.0,
            "fail_rate": 0.0,
            "avg_final_balance": STARTING_BALANCE,
            "avg_max_drawdown": 0.0,
            "avg_trades": 0,
        }

    max_start = len(daily_df) - window_days - 200
    if max_start <= 0:
        max_start = 1

    results = []
    rng = np.random.RandomState(42)

    for _ in range(n_simulations):
        start_idx = rng.randint(0, max_start)
        end_idx = start_idx + 200 + window_days
        window_df = daily_df.iloc[start_idx:end_idx].reset_index(drop=True)
        result = run_backtest(engine, symbol, window_df, strategy_name)
        results.append(result)

    passes = sum(1 for r in results if r.challenge_passed)
    fails = sum(1 for r in results if r.challenge_failed)

    return {
        "strategy_name": strategy_name,
        "n_simulations": n_simulations,
        "pass_rate": passes / n_simulations,
        "fail_rate": fails / n_simulations,
        "avg_final_balance": np.mean([r.final_balance for r in results]),
        "avg_max_drawdown": np.mean([r.max_drawdown_pct for r in results]),
        "avg_trades": np.mean([r.total_trades for r in results]),
        "median_total_pnl": np.median([r.total_pnl for r in results]),
        "results": results,
    }


def compare_strategies(
    conservative_engine: StrategyLike,
    challenge_engine: StrategyLike,
    symbol: str,
    daily_df: pd.DataFrame,
    n_simulations: int = 100,
    window_days: int = 90,
) -> dict:
    con = run_monte_carlo(
        conservative_engine, symbol, daily_df,
        "Conservative", n_simulations, window_days,
    )
    cha = run_monte_carlo(
        challenge_engine, symbol, daily_df,
        "Challenge", n_simulations, window_days,
    )
    return {"conservative": con, "challenge": cha}
