"""Regime analysis on historical data."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import numpy as np

from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from research.schema import to_engine_df


@dataclass
class RegimeStats:
    asset: str
    total_days: int
    regime_counts: dict[str, int]
    regime_pcts: dict[str, float]
    avg_duration: dict[str, float]
    transition_counts: int
    transitions: list[tuple[str, str, int]]


def analyze_regimes(asset: str, df: pd.DataFrame) -> RegimeStats:
    engine_df = to_engine_df(df)
    indicators = compute_indicators(engine_df)

    regimes = []
    for i in range(len(indicators)):
        row = indicators.iloc[i]
        regime = classify_regime(row)
        regimes.append(regime.value)

    regime_series = pd.Series(regimes, index=indicators.index)
    total = len(regime_series)

    counts = {}
    for r in MarketRegime:
        counts[r.value] = (regime_series == r.value).sum()

    pcts = {k: round(v / total * 100, 1) if total > 0 else 0 for k, v in counts.items()}

    runs = []
    current_regime = regime_series.iloc[0]
    run_len = 1
    for i in range(1, len(regime_series)):
        if regime_series.iloc[i] == current_regime:
            run_len += 1
        else:
            runs.append((current_regime, run_len))
            current_regime = regime_series.iloc[i]
            run_len = 1
    runs.append((current_regime, run_len))

    avg_dur = {}
    for r in MarketRegime:
        r_runs = [length for name, length in runs if name == r.value]
        avg_dur[r.value] = round(np.mean(r_runs), 1) if r_runs else 0

    transitions = []
    transition_count = 0
    for i in range(1, len(runs)):
        prev_regime = runs[i - 1][0]
        curr_regime = runs[i][0]
        transitions.append((prev_regime, curr_regime, 1))
        transition_count += 1

    trans_summary = {}
    for prev_r, curr_r, _ in transitions:
        key = (prev_r, curr_r)
        trans_summary[key] = trans_summary.get(key, 0) + 1

    return RegimeStats(
        asset=asset,
        total_days=total,
        regime_counts=counts,
        regime_pcts=pcts,
        avg_duration=avg_dur,
        transition_counts=transition_count,
        transitions=[(k[0], k[1], v) for k, v in trans_summary.items()],
    )


def format_regime_report(stats: RegimeStats) -> str:
    lines = [f"Regime Analysis: {stats.asset}", f"Total days: {stats.total_days}", ""]
    lines.append("Regime Distribution:")
    for regime in ["TREND", "CHOP", "LOWVOL", "PANIC", "DATA_INSUFFICIENT"]:
        count = stats.regime_counts.get(regime, 0)
        pct = stats.regime_pcts.get(regime, 0)
        avg = stats.avg_duration.get(regime, 0)
        lines.append(f"  {regime:20s}  {count:4d} days  ({pct:5.1f}%)  avg duration: {avg:.1f}d")

    lines.append(f"\nTotal regime transitions: {stats.transition_counts}")
    if stats.transitions:
        lines.append("Top transitions:")
        sorted_trans = sorted(stats.transitions, key=lambda x: x[2], reverse=True)
        for prev_r, curr_r, count in sorted_trans[:10]:
            lines.append(f"  {prev_r} → {curr_r}: {count}")

    return "\n".join(lines)
