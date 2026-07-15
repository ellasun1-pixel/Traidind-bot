"""Performance metrics for backtest results."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from research.backtest_engine import BacktestResult, DayState


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    total_return_usd: float
    num_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float
    profit_factor: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    sharpe_ratio: float
    sortino_ratio: float
    avg_holding_days: float
    capital_utilization: float
    pct_time_in_cash: float
    final_equity: float


def compute_metrics(result: BacktestResult) -> PerformanceMetrics:
    trades = result.trades
    curve = result.equity_curve
    starting = result.config.starting_balance

    if not curve:
        return _empty_metrics(starting)

    final_equity = curve[-1].equity
    total_return_usd = final_equity - starting
    total_return_pct = (total_return_usd / starting) * 100

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    num_trades = len(trades)
    win_rate = len(wins) / num_trades * 100 if num_trades > 0 else 0
    avg_win = np.mean([t.pnl for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl for t in losses]) if losses else 0

    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

    expectancy = np.mean([t.pnl for t in trades]) if trades else 0

    peak = starting
    max_dd_usd = 0
    max_dd_pct = 0
    for day in curve:
        if day.equity > peak:
            peak = day.equity
        dd_usd = peak - day.equity
        dd_pct = dd_usd / peak * 100 if peak > 0 else 0
        max_dd_usd = max(max_dd_usd, dd_usd)
        max_dd_pct = max(max_dd_pct, dd_pct)

    daily_returns = []
    for i in range(1, len(curve)):
        prev_eq = curve[i - 1].equity
        curr_eq = curve[i].equity
        if prev_eq > 0:
            daily_returns.append((curr_eq - prev_eq) / prev_eq)

    if daily_returns and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)
    else:
        sharpe = 0

    neg_returns = [r for r in daily_returns if r < 0]
    if neg_returns and np.std(neg_returns) > 0:
        sortino = np.mean(daily_returns) / np.std(neg_returns) * np.sqrt(252)
    else:
        sortino = 0

    holding_days = [t.holding_days for t in trades if t.holding_days > 0]
    avg_holding = np.mean(holding_days) if holding_days else 0

    days_with_positions = sum(1 for d in curve if d.open_positions > 0)
    total_days = len(curve)
    capital_util = days_with_positions / total_days * 100 if total_days > 0 else 0
    pct_cash = 100 - capital_util

    return PerformanceMetrics(
        total_return_pct=round(total_return_pct, 2),
        total_return_usd=round(total_return_usd, 2),
        num_trades=num_trades,
        win_rate=round(win_rate, 1),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        expectancy=round(expectancy, 2),
        profit_factor=round(profit_factor, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        max_drawdown_usd=round(max_dd_usd, 2),
        sharpe_ratio=round(sharpe, 2),
        sortino_ratio=round(sortino, 2),
        avg_holding_days=round(avg_holding, 1),
        capital_utilization=round(capital_util, 1),
        pct_time_in_cash=round(pct_cash, 1),
        final_equity=round(final_equity, 2),
    )


def _empty_metrics(starting: float) -> PerformanceMetrics:
    return PerformanceMetrics(
        total_return_pct=0, total_return_usd=0, num_trades=0,
        win_rate=0, avg_win=0, avg_loss=0, expectancy=0, profit_factor=0,
        max_drawdown_pct=0, max_drawdown_usd=0, sharpe_ratio=0, sortino_ratio=0,
        avg_holding_days=0, capital_utilization=0, pct_time_in_cash=100,
        final_equity=starting,
    )


def format_metrics(m: PerformanceMetrics) -> str:
    lines = [
        "Performance Metrics",
        "=" * 40,
        f"  Final equity:        ${m.final_equity:.2f}",
        f"  Total return:        {m.total_return_pct:+.2f}% (${m.total_return_usd:+.2f})",
        f"  Trades:              {m.num_trades}",
        f"  Win rate:            {m.win_rate:.1f}%",
        f"  Avg win:             ${m.avg_win:.2f}",
        f"  Avg loss:            ${m.avg_loss:.2f}",
        f"  Expectancy:          ${m.expectancy:.2f}",
        f"  Profit factor:       {m.profit_factor:.2f}",
        f"  Max drawdown:        {m.max_drawdown_pct:.2f}% (${m.max_drawdown_usd:.2f})",
        f"  Sharpe ratio:        {m.sharpe_ratio:.2f}",
        f"  Sortino ratio:       {m.sortino_ratio:.2f}",
        f"  Avg holding:         {m.avg_holding_days:.1f} days",
        f"  Capital utilization: {m.capital_utilization:.1f}%",
        f"  Time in cash:        {m.pct_time_in_cash:.1f}%",
    ]
    return "\n".join(lines)
