"""Run a detailed trade replay on synthetic data that triggers a genuine TREND signal.

Generates realistic price data where TREND conditions (ER20>=0.35, close>EMA200,
EMA50>EMA200) are met organically through the real classify_regime() function.
No hardcoded regime overrides — the strategy engine decides.

Usage:
    python -m research.run_trade_replay
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research.backtest_engine import (
    HistoricalBacktester,
    ExecutionConfig,
    format_trade_replay,
)
from research.schema import make_canonical


def generate_trend_data(
    n_total: int = 500,
    base_price: float = 100.0,
    seed: int = 2024,
) -> pd.DataFrame:
    """Generate synthetic daily OHLCV with a sustained uptrend phase.

    Structure:
    - Days 0-249:   Gradual uptrend (+0.05%/day) to establish EMA200 baseline
    - Days 250-279: Acceleration (+0.4%/day, low noise) — pushes ER20 above 0.35
                    and creates the EMA50 > EMA200 golden cross
    - Days 280-350: Continued trend then mean-reversion to test stop/TP
    - Days 350-499: Sideways/slight decline
    """
    rng = np.random.RandomState(seed)
    prices = np.zeros(n_total)
    prices[0] = base_price

    for i in range(1, n_total):
        if i < 250:
            drift = 0.0005
            noise = 0.008
        elif i < 280:
            drift = 0.004
            noise = 0.005
        elif i < 320:
            drift = 0.002
            noise = 0.008
        elif i < 360:
            drift = -0.001
            noise = 0.01
        else:
            drift = 0.0001
            noise = 0.012

        ret = drift + rng.normal(0, noise)
        prices[i] = prices[i - 1] * (1 + ret)

    highs = prices * (1 + rng.uniform(0.002, 0.012, n_total))
    lows = prices * (1 - rng.uniform(0.002, 0.012, n_total))
    opens = np.roll(prices, 1)
    opens[0] = base_price

    dates = pd.date_range("2024-01-01", periods=n_total, freq="1D")

    df = pd.DataFrame({
        "asset": "SIM/USD",
        "timestamp": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": rng.uniform(1000, 5000, n_total),
        "source": "synthetic",
    })
    return make_canonical(df)


def main():
    print("Generating synthetic trend data...")
    df = generate_trend_data()
    print(f"  {len(df)} daily candles, {df['timestamp'].iloc[0].date()} to {df['timestamp'].iloc[-1].date()}")
    print(f"  Price range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")

    from src.strategy.indicators import compute_indicators
    from src.strategy.regime import classify_regime
    from research.schema import to_engine_df

    engine_df = to_engine_df(df)
    enriched = compute_indicators(engine_df)

    print("\nScanning for TREND days...")
    trend_days = []
    for i in range(200, len(enriched)):
        row = enriched.iloc[i]
        regime = classify_regime(row)
        if regime.value == "TREND":
            date_str = str(row["open_time"])[:10]
            er20 = float(row["er20"])
            ema50 = float(row["ema50"])
            ema200 = float(row["ema200"])
            close = float(row["close"])
            trend_days.append((i, date_str, close, er20, ema50, ema200))

    if not trend_days:
        print("  No TREND days found — adjusting data generation parameters.")
        sys.exit(1)

    print(f"  Found {len(trend_days)} TREND days!")
    print(f"\n  First 10 TREND days:")
    print(f"  {'Day':>4} {'Date':<12} {'Close':>10} {'ER20':>8} {'EMA50':>10} {'EMA200':>10}")
    for idx, date, close, er20, ema50, ema200 in trend_days[:10]:
        print(f"  {idx:4d} {date:<12} ${close:>9.2f} {er20:>8.4f} ${ema50:>9.2f} ${ema200:>9.2f}")

    print(f"\n{'='*80}")
    print("Running backtest with conservative strategy...")
    print(f"{'='*80}\n")

    config = ExecutionConfig()
    bt = HistoricalBacktester(strategy="conservative", config=config)
    result = bt.run("SIM/USD", df)

    print(f"Backtest period: {result.start_date} to {result.end_date}")
    print(f"Total trades: {len(result.trades)}")

    if not result.trades:
        print("\nNo trades executed. Signal funnel summary:")
        by_type = {}
        by_regime = {}
        for entry in result.signal_funnel:
            by_type[entry.signal_type] = by_type.get(entry.signal_type, 0) + 1
            by_regime[entry.regime] = by_regime.get(entry.regime, 0) + 1
        print("  By signal type:")
        for t, c in sorted(by_type.items()):
            print(f"    {t:20s} {c:4d}")
        print("  By regime:")
        for r, c in sorted(by_regime.items()):
            print(f"    {r:20s} {c:4d}")

        print("\n  Checking why BUY didn't fire on TREND days...")
        print("  (The strategy engine has additional filters beyond regime: "
              "candle confirmation, vertical spike, position limits, balance guards)")

        for entry in result.signal_funnel:
            if entry.regime == "TREND":
                print(f"    {entry.date}: regime=TREND signal={entry.signal_type} "
                      f"reason={entry.reason[:60]}")
        sys.exit(0)

    for trade in result.trades:
        print(f"\n{'='*80}")
        replay = format_trade_replay(result, trade.trade_id,
                                     context_days_before=5,
                                     context_days_after=3)
        print(replay)

    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    wins = sum(1 for t in result.trades if t.pnl > 0)
    losses = sum(1 for t in result.trades if t.pnl <= 0)
    total_pnl = sum(t.pnl for t in result.trades)
    print(f"  Trades: {len(result.trades)}  (W: {wins} / L: {losses})")
    print(f"  Total P&L: ${total_pnl:+.2f}")
    print(f"  Final equity: ${result.equity_curve[-1].equity:.2f}" if result.equity_curve else "")

    by_regime = {}
    for entry in result.signal_funnel:
        by_regime[entry.regime] = by_regime.get(entry.regime, 0) + 1
    print(f"\n  Regime distribution:")
    for r, c in sorted(by_regime.items()):
        total = len(result.signal_funnel)
        print(f"    {r:20s} {c:4d} ({c/total*100:.1f}%)")


if __name__ == "__main__":
    main()
