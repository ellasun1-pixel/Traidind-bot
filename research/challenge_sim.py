"""Challenge simulation using chronological block bootstrap.

Preserves market dependence by sampling contiguous blocks of trades
rather than independent random draws.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from research.backtest_engine import BacktestResult

logger = logging.getLogger(__name__)


@dataclass
class ChallengeSimResult:
    n_simulations: int
    prob_win: float
    prob_loss: float
    prob_neither: float
    median_days_to_boundary: float
    median_final_balance: float
    pct5_final_balance: float
    pct95_final_balance: float
    all_final_balances: list[float]
    all_outcomes: list[str]


def run_challenge_simulation(
    result: BacktestResult,
    n_sims: int = 1000,
    block_size: int = 5,
    starting_balance: float = 1000.0,
    win_level: float = 1120.0,
    loss_level: float = 950.0,
    max_days: int = 365,
    rng_seed: int | None = None,
) -> ChallengeSimResult:
    rng = np.random.default_rng(rng_seed)

    trade_pnls = [t.pnl for t in result.trades]
    trade_holding = [max(t.holding_days, 1) for t in result.trades]

    if not trade_pnls:
        return ChallengeSimResult(
            n_simulations=n_sims, prob_win=0, prob_loss=0, prob_neither=100.0,
            median_days_to_boundary=max_days, median_final_balance=starting_balance,
            pct5_final_balance=starting_balance, pct95_final_balance=starting_balance,
            all_final_balances=[starting_balance] * n_sims,
            all_outcomes=["NEITHER"] * n_sims,
        )

    n_trades = len(trade_pnls)
    pnl_arr = np.array(trade_pnls)
    hold_arr = np.array(trade_holding)

    outcomes = []
    final_balances = []
    days_to_boundary = []

    for _ in range(n_sims):
        balance = starting_balance
        total_days = 0
        outcome = "NEITHER"

        while total_days < max_days:
            start = rng.integers(0, max(1, n_trades - block_size + 1))
            block_end = min(start + block_size, n_trades)

            for j in range(start, block_end):
                balance += pnl_arr[j]
                total_days += hold_arr[j]

                if balance >= win_level:
                    outcome = "WIN"
                    break
                if balance <= loss_level:
                    outcome = "LOSS"
                    break

            if outcome != "NEITHER":
                break

        outcomes.append(outcome)
        final_balances.append(balance)
        days_to_boundary.append(total_days)

    win_count = outcomes.count("WIN")
    loss_count = outcomes.count("LOSS")
    neither_count = outcomes.count("NEITHER")

    boundary_days = [d for d, o in zip(days_to_boundary, outcomes) if o != "NEITHER"]

    return ChallengeSimResult(
        n_simulations=n_sims,
        prob_win=round(win_count / n_sims * 100, 1),
        prob_loss=round(loss_count / n_sims * 100, 1),
        prob_neither=round(neither_count / n_sims * 100, 1),
        median_days_to_boundary=float(np.median(boundary_days)) if boundary_days else max_days,
        median_final_balance=round(float(np.median(final_balances)), 2),
        pct5_final_balance=round(float(np.percentile(final_balances, 5)), 2),
        pct95_final_balance=round(float(np.percentile(final_balances, 95)), 2),
        all_final_balances=[round(b, 2) for b in final_balances],
        all_outcomes=outcomes,
    )


def format_challenge_sim(sim: ChallengeSimResult) -> str:
    lines = [
        "Challenge Simulation Results",
        "=" * 40,
        f"  Simulations:         {sim.n_simulations}",
        f"  P(reach $1120):      {sim.prob_win:.1f}%",
        f"  P(hit $950):         {sim.prob_loss:.1f}%",
        f"  P(neither):          {sim.prob_neither:.1f}%",
        f"  Median days:         {sim.median_days_to_boundary:.0f}",
        f"  Median final:        ${sim.median_final_balance:.2f}",
        f"  5th pct:             ${sim.pct5_final_balance:.2f}",
        f"  95th pct:            ${sim.pct95_final_balance:.2f}",
    ]
    return "\n".join(lines)
