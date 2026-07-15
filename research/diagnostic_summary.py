"""Compact diagnostic summary — outputs ~60 lines of key numbers.

Usage:
    python -m research.diagnostic_summary
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from research.schema import load_data, to_engine_df, ASSETS
from research.walk_forward import create_splits
from research.backtest_engine import HistoricalBacktester, ExecutionConfig
from research.metrics import compute_metrics
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, regime_nan_fields, MarketRegime
from src.config import settings

DATA_DIR = Path("research/data")


def analyze_asset(asset: str, df: pd.DataFrame, warmup: int = 252):
    splits = create_splits(df, warmup_candles=warmup)
    results = []

    for split in splits:
        split_df = df.iloc[split.start_idx:split.end_idx].copy().reset_index(drop=True)
        engine_df = to_engine_df(split_df)
        daily = compute_indicators(engine_df)
        n = len(daily) - warmup - 1
        if n <= 0:
            continue

        regime_counts = {"TREND": 0, "CHOP": 0, "LOWVOL": 0, "PANIC": 0, "DATA_INSUFFICIENT": 0}
        funnel = {"start": n, "valid": 0, "regime": 0, "confirm": 0, "spike": 0}
        killed = {"regime": 0, "confirm": 0, "spike": 0}
        overlap_count = 0

        for i in range(warmup, len(daily) - 1):
            row = daily.iloc[i]
            prev = daily.iloc[i - 1]
            close = float(row["close"])
            ema200 = float(row.get("ema200", 0) or 0)
            er20 = float(row.get("er20", 0) or 0)
            rvol = float(row.get("rvol", 0) or 0)
            rvol_pct25 = float(row.get("rvol_pct25", 0) or 0)
            pcs = float(row.get("price_change_short", 0) or 0)
            prev_close = float(prev.get("close", 0) or 0)
            prev_ema50 = float(prev.get("ema50", 0) or 0)

            regime = classify_regime(row)
            regime_counts[regime.value] = regime_counts.get(regime.value, 0) + 1

            if (0.30 <= er20 < 0.35) and (rvol_pct25 > 0 and rvol <= rvol_pct25) and (close > ema200):
                overlap_count += 1

            nans = regime_nan_fields(row)
            if nans:
                continue
            funnel["valid"] += 1

            regime_ok = regime not in (MarketRegime.PANIC, MarketRegime.DATA_INSUFFICIENT)
            if not regime_ok:
                killed["regime"] += 1
                continue
            funnel["regime"] += 1

            candle_ok = (prev_close > prev_ema50) if (prev_close > 0 and prev_ema50 > 0) else False
            if not candle_ok:
                killed["confirm"] += 1
                continue
            funnel["confirm"] += 1

            spike_ok = abs(pcs) <= settings.vertical_spike_pct
            if not spike_ok:
                killed["spike"] += 1
                continue
            funnel["spike"] += 1

        # Run backtest
        bt = HistoricalBacktester(strategy="conservative", config=ExecutionConfig())
        bt_result = bt.run(asset, split_df, warmup_candles=warmup)
        m = compute_metrics(bt_result)

        results.append({
            "period": split.name,
            "dates": f"{split.start_date}→{split.end_date}",
            "candles": n,
            "regimes": regime_counts,
            "funnel": funnel,
            "killed": killed,
            "overlap": overlap_count,
            "trades": m.num_trades,
            "win_rate": m.win_rate,
            "expectancy": m.expectancy,
            "pf": m.profit_factor,
            "return_pct": m.total_return_pct,
            "max_dd": m.max_drawdown_pct,
        })

    return results


def main():
    data_files = sorted(DATA_DIR.glob("*.csv")) + sorted(DATA_DIR.glob("*.json"))
    data_files = [f for f in data_files if f.name != ".gitkeep"]
    if not data_files:
        print("ERROR: No data in research/data/. Run: python -m research.fetch_data --provider kraken --days 730")
        sys.exit(1)

    print(f"TP={settings.take_profit_risk_multiple} Risk={settings.risk_per_trade_pct_default} Spike={settings.vertical_spike_pct} MaxPos={settings.max_open_positions}")
    print()

    total_killed = {"regime": 0, "ema200": 0, "confirm": 0, "spike": 0}

    for asset_name in ASSETS:
        safe = asset_name.replace("/", "_")
        candidates = [f for f in data_files if safe in f.stem or safe.lower() in f.stem.lower()]
        if not candidates:
            print(f"{asset_name}: NO DATA")
            continue

        df = load_data(candidates[0])
        asset_df = df[df["asset"] == asset_name].copy()
        if asset_df.empty:
            asset_df = df.copy()

        print(f"=== {asset_name} ({len(asset_df)} candles) ===")
        results = analyze_asset(asset_name, asset_df)

        for r in results:
            rc = r["regimes"]
            f = r["funnel"]
            k = r["killed"]
            print(f"  {r['period']:5s} {r['dates']}  candles={r['candles']}")
            print(f"    regimes: T={rc['TREND']} C={rc['CHOP']} L={rc['LOWVOL']} P={rc['PANIC']} D={rc['DATA_INSUFFICIENT']}")
            print(f"    funnel:  {f['start']}→valid {f['valid']}→regime {f['regime']}→confirm {f['confirm']}→spike {f['spike']}")
            print(f"    killed:  regime={k['regime']} confirm={k['confirm']} spike={k['spike']}")
            print(f"    overlap: {r['overlap']}  trades={r['trades']} WR={r['win_rate']:.0f}% exp=${r['expectancy']:.2f} PF={r['pf']:.2f} ret={r['return_pct']:+.2f}% dd={r['max_dd']:.2f}%")

            for key in total_killed:
                total_killed[key] += k[key]

    print()
    print(f"=== TOTAL KILLS ACROSS ALL ASSETS/PERIODS ===")
    total = sum(total_killed.values())
    for key in ["regime", "ema200", "confirm", "spike"]:
        v = total_killed[key]
        pct = v / total * 100 if total > 0 else 0
        print(f"  {key:10s}: {v:5d} ({pct:5.1f}%)")
    print(f"  {'total':10s}: {total:5d}")


if __name__ == "__main__":
    main()
