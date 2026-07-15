"""Run the complete Phase 2 historical validation study in one command.

Usage:
    python -m research.run_full_study --provider kraken --days 730

This safely runs, in order:
    1. fetch_data    — download daily OHLCV from Kraken or Coinbase
    2. validate_data — verify data integrity before backtesting
    3. backtest      — conservative strategy on all assets
    4. walk-forward  — train/validation/test evaluation
    5. challenge sim — block bootstrap challenge simulation
    6. report        — generate final report

Safety guarantees:
    - Research only — does not start any services or place any trades
    - No production database writes
    - Results written only to research/output/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_DIR = Path(__file__).parent / "output"


def _step(n: int, total: int, name: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Step {n}/{total}: {name}")
    print(f"{'=' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Run complete Phase 2 historical validation study",
    )
    parser.add_argument(
        "--provider", default="kraken", choices=["kraken", "coinbase"],
        help="Data provider (default: kraken)",
    )
    parser.add_argument(
        "--days", type=int, default=730,
        help="Days of history to fetch (default: 730)",
    )
    parser.add_argument(
        "--assets", default=None,
        help="Comma-separated assets (default: all five)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: research/output)",
    )
    parser.add_argument(
        "--challenge-sims", type=int, default=1000,
        help="Number of challenge simulations (default: 1000)",
    )
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="Skip data fetching (use existing files in research/data/)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    output_dir = Path(args.output_dir) if args.output_dir else OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    total_steps = 5 if args.skip_fetch else 6
    step = 0
    t0 = time.time()

    print("=" * 60)
    print("  PHASE 2 — HISTORICAL MARKET VALIDATION STUDY")
    print(f"  Provider: {args.provider}  |  Days: {args.days}")
    print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    asset_list = args.assets.split(",") if args.assets else None

    if not args.skip_fetch:
        step += 1
        _step(step, total_steps, "Fetching historical data")

        from research.fetch_data import fetch_all, save_all

        datasets = asyncio.run(fetch_all(args.provider, asset_list, args.days))
        if not datasets:
            print("ERROR: No data fetched. Check network connectivity.")
            sys.exit(1)

        saved = save_all(datasets, DATA_DIR)
        print(f"Saved {len(saved)} files to {DATA_DIR}")

    step += 1
    _step(step, total_steps, "Validating data")

    from research.validate_data import validate
    from research.schema import load_data

    data_files = sorted(DATA_DIR.glob("*.csv")) + sorted(DATA_DIR.glob("*.json"))
    data_files = [f for f in data_files if f.name != ".gitkeep"]

    if not data_files:
        print(f"ERROR: No data files in {DATA_DIR}")
        print("Run with --provider to fetch, or place CSV files manually.")
        sys.exit(1)

    datasets = {}
    for path in data_files:
        df = load_data(path)
        report = validate(df)
        print(report)
        if not report.passed:
            print(f"\nERROR: {path.name} failed validation. Fix errors first.")
            sys.exit(1)
        asset = df["asset"].iloc[0]
        if asset_list and asset not in asset_list:
            continue
        datasets[asset] = df

    print(f"\nAll {len(datasets)} files passed validation.")

    step += 1
    _step(step, total_steps, "Running conservative backtest")

    from research.backtest_engine import HistoricalBacktester, ExecutionConfig
    from research.metrics import compute_metrics, format_metrics
    from research.regime_analysis import analyze_regimes, format_regime_report
    from research.run_backtest import _compute_funnel, _trade_to_dict

    config = ExecutionConfig()
    all_backtest_results = {}

    for asset, df in datasets.items():
        print(f"\n--- {asset} ---")
        bt = HistoricalBacktester(strategy="conservative", config=config)
        result = bt.run(asset, df)
        metrics = compute_metrics(result)
        regime_stats = analyze_regimes(asset, df)

        print(format_metrics(metrics))
        print()
        print(format_regime_report(regime_stats))

        backtest_output = {
            "asset": asset,
            "strategy": "conservative",
            "start_date": result.start_date,
            "end_date": result.end_date,
            "metrics": metrics.__dict__,
            "regime": {
                "counts": regime_stats.regime_counts,
                "percentages": regime_stats.regime_pcts,
                "avg_duration": regime_stats.avg_duration,
                "transitions": regime_stats.transition_counts,
            },
            "signal_funnel": _compute_funnel(result),
            "trades": [_trade_to_dict(t) for t in result.trades],
        }
        all_backtest_results[asset] = backtest_output

        safe = asset.replace("/", "_")
        out_path = output_dir / f"{safe}_conservative_backtest.json"
        with open(out_path, "w") as f:
            json.dump(backtest_output, f, indent=2, default=str)

    step += 1
    _step(step, total_steps, "Running walk-forward evaluation")

    from research.walk_forward import run_walk_forward, format_walk_forward
    from research.challenge_sim import run_challenge_simulation, format_challenge_sim

    all_wf_results = {}

    for asset, df in datasets.items():
        print(f"\n--- {asset} ---")
        wf = run_walk_forward(asset, df, strategy="conservative", config=config)
        print(format_walk_forward(wf))

        test_result = wf.results.get("test")
        if test_result and test_result.trades:
            print(f"\nChallenge simulation ({args.challenge_sims} runs):")
            sim = run_challenge_simulation(
                test_result,
                n_sims=args.challenge_sims,
                rng_seed=42,
            )
            print(format_challenge_sim(sim))

        wf_output = {
            "asset": asset,
            "strategy": "conservative",
            "mode": "fixed",
            "splits": [
                {
                    "name": s.name,
                    "start_date": s.start_date,
                    "end_date": s.end_date,
                    "metrics": wf.metrics[s.name].__dict__ if s.name in wf.metrics else None,
                }
                for s in wf.splits
            ],
        }
        all_wf_results[asset] = wf_output

        safe = asset.replace("/", "_")
        out_path = output_dir / f"{safe}_conservative_walkforward.json"
        with open(out_path, "w") as f:
            json.dump(wf_output, f, indent=2, default=str)

    step += 1
    _step(step, total_steps, "Generating final report")

    from research.generate_report import main as generate_report_main
    sys.argv = ["generate_report", f"--output-dir={output_dir}"]
    generate_report_main()

    elapsed = time.time() - t0
    report_path = output_dir / "phase2_report.txt"

    print(f"\n{'=' * 60}")
    print(f"  STUDY COMPLETE")
    print(f"  Elapsed: {elapsed:.0f} seconds")
    print(f"  Assets: {', '.join(datasets.keys())}")
    print(f"  Results: {output_dir}")
    print(f"  Report:  {report_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
