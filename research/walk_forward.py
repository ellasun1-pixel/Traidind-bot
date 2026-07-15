"""Walk-forward evaluation with train/validation/test splits.

Usage:
    python -m research.run_walk_forward [--data-dir research/data] [--strategy conservative]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from research.backtest_engine import HistoricalBacktester, BacktestResult, ExecutionConfig
from research.metrics import compute_metrics, PerformanceMetrics

logger = logging.getLogger(__name__)


@dataclass
class WalkForwardSplit:
    name: str
    start_idx: int
    end_idx: int
    start_date: str
    end_date: str


@dataclass
class WalkForwardResult:
    splits: list[WalkForwardSplit]
    results: dict[str, BacktestResult]
    metrics: dict[str, PerformanceMetrics]


def create_splits(
    df: pd.DataFrame,
    train_pct: float = 0.60,
    val_pct: float = 0.20,
    test_pct: float = 0.20,
    warmup_candles: int = 252,
) -> list[WalkForwardSplit]:
    n = len(df)
    usable = n - warmup_candles
    if usable <= 0:
        raise ValueError(f"Not enough data: {n} candles, need >{warmup_candles}")

    train_end = warmup_candles + int(usable * train_pct)
    val_end = train_end + int(usable * val_pct)

    splits = [
        WalkForwardSplit(
            name="train",
            start_idx=0,
            end_idx=train_end,
            start_date=str(df["timestamp"].iloc[warmup_candles])[:10],
            end_date=str(df["timestamp"].iloc[train_end - 1])[:10],
        ),
        WalkForwardSplit(
            name="validation",
            start_idx=train_end - warmup_candles,
            end_idx=val_end,
            start_date=str(df["timestamp"].iloc[train_end])[:10],
            end_date=str(df["timestamp"].iloc[val_end - 1])[:10],
        ),
        WalkForwardSplit(
            name="test",
            start_idx=val_end - warmup_candles,
            end_idx=n,
            start_date=str(df["timestamp"].iloc[val_end])[:10],
            end_date=str(df["timestamp"].iloc[-1])[:10],
        ),
    ]
    return splits


def create_rolling_splits(
    df: pd.DataFrame,
    window_days: int = 365,
    step_days: int = 90,
    warmup_candles: int = 252,
) -> list[WalkForwardSplit]:
    n = len(df)
    splits = []
    fold = 0

    start = 0
    while start + warmup_candles + window_days <= n:
        end = start + warmup_candles + window_days
        fold += 1
        splits.append(WalkForwardSplit(
            name=f"fold_{fold}",
            start_idx=start,
            end_idx=min(end, n),
            start_date=str(df["timestamp"].iloc[start + warmup_candles])[:10],
            end_date=str(df["timestamp"].iloc[min(end, n) - 1])[:10],
        ))
        start += step_days

    return splits


def run_walk_forward(
    asset: str,
    df: pd.DataFrame,
    strategy: str = "conservative",
    config: ExecutionConfig | None = None,
    mode: str = "fixed",
    warmup_candles: int = 252,
) -> WalkForwardResult:
    if mode == "fixed":
        splits = create_splits(df, warmup_candles=warmup_candles)
    else:
        splits = create_rolling_splits(df, warmup_candles=warmup_candles)

    results = {}
    metrics = {}

    for split in splits:
        logger.info("Running %s split: %s to %s", split.name, split.start_date, split.end_date)
        split_df = df.iloc[split.start_idx:split.end_idx].copy().reset_index(drop=True)

        bt = HistoricalBacktester(strategy=strategy, config=config)
        result = bt.run(asset, split_df, warmup_candles)
        results[split.name] = result
        metrics[split.name] = compute_metrics(result)

    return WalkForwardResult(splits=splits, results=results, metrics=metrics)


def format_walk_forward(wf: WalkForwardResult) -> str:
    lines = ["Walk-Forward Results", "=" * 60]

    for split in wf.splits:
        m = wf.metrics.get(split.name)
        if not m:
            continue
        lines.append(f"\n{split.name.upper()} ({split.start_date} to {split.end_date}):")
        lines.append(f"  Trades: {m.num_trades}  Win rate: {m.win_rate:.1f}%  "
                      f"Expectancy: ${m.expectancy:.2f}  PF: {m.profit_factor:.2f}")
        lines.append(f"  Return: {m.total_return_pct:+.2f}%  MaxDD: {m.max_drawdown_pct:.2f}%  "
                      f"Sharpe: {m.sharpe_ratio:.2f}")

    return "\n".join(lines)
