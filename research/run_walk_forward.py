"""Run walk-forward evaluation.

Usage:
    python -m research.run_walk_forward [--data-dir research/data] [--strategy conservative]
        [--mode fixed|rolling] [--assets BTC/USD]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from research.backtest_engine import ExecutionConfig
from research.challenge_sim import run_challenge_simulation, format_challenge_sim
from research.metrics import format_metrics
from research.schema import load_data
from research.walk_forward import run_walk_forward, format_walk_forward

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Run walk-forward evaluation")
    parser.add_argument("--data-dir", default="research/data")
    parser.add_argument("--strategy", default="conservative", choices=["conservative", "challenge"])
    parser.add_argument("--mode", default="fixed", choices=["fixed", "rolling"])
    parser.add_argument("--assets", default=None, help="Comma-separated: BTC/USD,ETH/USD")
    parser.add_argument("--output-dir", default="research/output")
    parser.add_argument("--challenge-sims", type=int, default=1000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    config = ExecutionConfig()

    if args.assets:
        assets = args.assets.split(",")
    else:
        csv_files = sorted(data_dir.glob("*.csv"))
        assets = [f.stem.replace("_", "/") for f in csv_files if f.name != ".gitkeep"]

    if not assets:
        print(f"No data files found in {data_dir}")
        sys.exit(1)

    for asset in assets:
        safe = asset.replace("/", "_")
        data_path = data_dir / f"{safe}.csv"
        if not data_path.exists():
            data_path = data_dir / f"{safe}.json"
        if not data_path.exists():
            logger.warning("No data file for %s, skipping", asset)
            continue

        df = load_data(data_path)
        logger.info("Loaded %d candles for %s", len(df), asset)

        wf = run_walk_forward(asset, df, args.strategy, config, args.mode)

        print(f"\n{'=' * 60}")
        print(f"Walk-Forward: {asset} | Strategy: {args.strategy} | Mode: {args.mode}")
        print(f"{'=' * 60}")
        print(format_walk_forward(wf))

        test_result = wf.results.get("test")
        if test_result and test_result.trades:
            print(f"\nChallenge Simulation (from TEST period, {args.challenge_sims} sims):")
            sim = run_challenge_simulation(
                test_result,
                n_sims=args.challenge_sims,
                rng_seed=42,
            )
            print(format_challenge_sim(sim))

        out = {
            "asset": asset,
            "strategy": args.strategy,
            "mode": args.mode,
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

        out_path = output_dir / f"{safe}_{args.strategy}_walkforward.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        logger.info("Saved to %s", out_path)


if __name__ == "__main__":
    main()
