"""Deep diagnostic audit: trace every entry filter individually per asset/period.

Outputs per-filter rejection rates, regime distributions, and detailed
trade logs to identify the root cause of 0 trades in validation/test.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research.schema import load_data, ASSETS
from research.walk_forward import create_splits
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from src.config import settings

logging.basicConfig(level=logging.WARNING)


def run_filter_audit(asset: str, df: pd.DataFrame, warmup: int = 252) -> dict:
    """Trace each buy-condition filter individually across the dataframe."""
    from research.schema import to_engine_df
    engine_df = to_engine_df(df)
    daily = compute_indicators(engine_df)

    results = []
    for i in range(warmup, len(daily) - 1):
        row = daily.iloc[i]
        prev = daily.iloc[i - 1] if i > 0 else row
        date = str(row.get("open_time", ""))[:10]
        close = float(row["close"])
        ema200 = float(row.get("ema200", 0) or 0)
        ema50 = float(row.get("ema50", 0) or 0)
        er20 = float(row.get("er20", 0) or 0)
        rvol = float(row.get("rvol", 0) or 0)
        rvol_pct25 = float(row.get("rvol_pct25", 0) or 0)
        rvol_median = float(row.get("rvol_median_252", 0) or 0)
        price_change_48h = float(row.get("price_change_48h", 0) or 0)
        price_change_short = float(row.get("price_change_short", 0) or 0)
        adx14 = float(row.get("adx14", 0) or 0)

        prev_close = float(prev.get("close", 0) or 0)
        prev_ema50 = float(prev.get("ema50", 0) or 0)

        regime = classify_regime(row)

        # Trace each filter independently
        f_regime_ok = regime not in (MarketRegime.PANIC, MarketRegime.DATA_INSUFFICIENT)
        f_not_panic = regime != MarketRegime.PANIC
        f_price_above_ema200 = close > ema200 if ema200 > 0 else False
        f_candle_confirm = (prev_close > prev_ema50) if (prev_close > 0 and prev_ema50 > 0) else False
        f_spike_ok = abs(price_change_short) <= settings.vertical_spike_pct
        f_regime_allows_buy = regime in (MarketRegime.TREND, MarketRegime.CHOP, MarketRegime.LOWVOL)

        # What WOULD the regime be if we checked TREND before LOWVOL?
        alt_regime = regime
        if regime == MarketRegime.LOWVOL and er20 >= 0.30 and close > ema200:
            alt_regime = MarketRegime.TREND

        # Combined: all filters pass
        all_pass = f_regime_ok and f_price_above_ema200 and f_candle_confirm and f_spike_ok

        results.append({
            "date": date,
            "close": close,
            "ema200": ema200,
            "ema50": ema50,
            "er20": er20,
            "rvol": rvol,
            "rvol_pct25": rvol_pct25,
            "adx14": adx14,
            "regime": regime.value,
            "alt_regime": alt_regime.value,
            "price_change_48h": price_change_48h,
            "price_change_short": price_change_short,
            "prev_close": prev_close,
            "prev_ema50": prev_ema50,
            "f_regime_ok": f_regime_ok,
            "f_price_above_ema200": f_price_above_ema200,
            "f_candle_confirm": f_candle_confirm,
            "f_spike_ok": f_spike_ok,
            "f_all_pass": all_pass,
        })

    return pd.DataFrame(results)


def print_filter_report(asset: str, audit_df: pd.DataFrame, period_name: str):
    n = len(audit_df)
    if n == 0:
        print(f"  {period_name}: 0 tradeable candles")
        return

    print(f"\n  {period_name.upper()} ({n} tradeable candles):")

    # Regime distribution
    regime_counts = audit_df["regime"].value_counts()
    print(f"    Regime distribution:")
    for regime, count in regime_counts.items():
        pct = count / n * 100
        print(f"      {regime:20s}: {count:4d} ({pct:5.1f}%)")

    # Alt-regime (if TREND checked before LOWVOL)
    alt_counts = audit_df["alt_regime"].value_counts()
    stolen = (audit_df["regime"] != audit_df["alt_regime"]).sum()
    if stolen > 0:
        print(f"    LOWVOL→TREND reclassifications if order fixed: {stolen}")
        print(f"    Alt regime distribution:")
        for regime, count in alt_counts.items():
            pct = count / n * 100
            print(f"      {regime:20s}: {count:4d} ({pct:5.1f}%)")

    # Individual filter pass rates
    print(f"    Filter pass rates:")
    for col, label in [
        ("f_regime_ok", "Regime allows trading    "),
        ("f_price_above_ema200", "Price > EMA200           "),
        ("f_candle_confirm", "Prev close > prev EMA50  "),
        ("f_spike_ok", "No vertical spike        "),
        ("f_all_pass", "ALL FILTERS PASS         "),
    ]:
        passing = audit_df[col].sum()
        pct = passing / n * 100
        print(f"      {label}: {passing:4d}/{n:4d} ({pct:5.1f}%)")

    # Filter rejection cascade (ordered)
    remaining = n
    print(f"    Filter cascade (sequential rejection):")
    cascade = [
        ("f_regime_ok", "Regime allows trading"),
        ("f_price_above_ema200", "Price > EMA200"),
        ("f_candle_confirm", "Prev close > prev EMA50"),
        ("f_spike_ok", "No vertical spike"),
    ]
    mask = pd.Series([True] * n, index=audit_df.index)
    for col, label in cascade:
        new_mask = mask & audit_df[col]
        rejected = mask.sum() - new_mask.sum()
        print(f"      After '{label}': {new_mask.sum():4d} remain ({rejected:4d} rejected)")
        mask = new_mask

    # Candle confirmation detail: when price > EMA200, how often does candle confirm fail?
    above_ema200 = audit_df[audit_df["f_price_above_ema200"]]
    if len(above_ema200) > 0:
        cc_fail = (~above_ema200["f_candle_confirm"]).sum()
        print(f"    When price > EMA200 ({len(above_ema200)} days): candle confirm fails {cc_fail} times ({cc_fail/len(above_ema200)*100:.1f}%)")

    # Days where ALL conditions met
    all_pass_days = audit_df[audit_df["f_all_pass"]]
    if len(all_pass_days) > 0:
        print(f"    Days with ALL filters passing: {len(all_pass_days)}")
        for _, row in all_pass_days.iterrows():
            print(f"      {row['date']}: regime={row['regime']} close={row['close']:.2f} er20={row['er20']:.3f} ema200={row['ema200']:.2f}")
    else:
        print(f"    *** NO DAYS with all filters passing ***")

    # Show ER20 distribution for days with price > EMA200
    if len(above_ema200) > 0:
        er20_vals = above_ema200["er20"]
        print(f"    ER20 stats (when price > EMA200): mean={er20_vals.mean():.3f} median={er20_vals.median():.3f} "
              f"min={er20_vals.min():.3f} max={er20_vals.max():.3f}")
        print(f"      er20 >= 0.30: {(er20_vals >= 0.30).sum()}, er20 >= 0.35: {(er20_vals >= 0.35).sum()}")


def main():
    data_dir = Path("research/data")
    if not data_dir.exists():
        print("ERROR: research/data directory not found. Run from project root after fetching data.")
        sys.exit(1)

    for asset_name in ASSETS:
        safe_name = asset_name.replace("/", "_")
        candidates = list(data_dir.glob(f"*{safe_name}*")) + list(data_dir.glob(f"*{safe_name.lower()}*"))
        if not candidates:
            print(f"\n{'='*70}")
            print(f"ASSET: {asset_name} — no data file found, skipping")
            continue

        data_path = candidates[0]
        print(f"\n{'='*70}")
        print(f"ASSET: {asset_name} (from {data_path.name})")
        print(f"{'='*70}")

        df = load_data(data_path)
        asset_df = df[df["asset"] == asset_name].copy()
        if asset_df.empty:
            asset_df = df.copy()

        print(f"Total candles: {len(asset_df)}")

        # Create walk-forward splits
        try:
            splits = create_splits(asset_df, warmup_candles=252)
        except ValueError as e:
            print(f"  Cannot create splits: {e}")
            continue

        # Full dataset audit
        audit_full = run_filter_audit(asset_name, asset_df)
        print_filter_report(asset_name, audit_full, "FULL DATASET")

        # Per-split audit
        for split in splits:
            split_df = asset_df.iloc[split.start_idx:split.end_idx].copy().reset_index(drop=True)
            audit_split = run_filter_audit(asset_name, split_df)
            print_filter_report(asset_name, audit_split, f"{split.name} ({split.start_date} to {split.end_date})")

    # Summary of key findings
    print(f"\n{'='*70}")
    print("DIAGNOSTIC SUMMARY")
    print(f"{'='*70}")
    print("""
Key questions answered by this audit:
1. Which filter rejects the most signals?
2. Is the regime overlap (LOWVOL stealing TREND) significant?
3. Is the candle confirmation filter (prev_close > prev_ema50) the bottleneck?
4. Are there ANY days in validation/test where all filters pass?
5. What is the ER20 distribution in periods where price > EMA200?
""")


if __name__ == "__main__":
    main()
