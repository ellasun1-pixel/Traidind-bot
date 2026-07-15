"""Run backtest on historical data.

Usage:
    python -m research.run_backtest [--data-dir research/data] [--strategy conservative]
        [--assets BTC/USD,ETH/USD] [--output-dir research/output]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from research.backtest_engine import HistoricalBacktester, ExecutionConfig
from research.metrics import compute_metrics, format_metrics
from research.regime_analysis import analyze_regimes, format_regime_report
from research.schema import load_data

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
OUTPUT_DIR = Path(__file__).parent / "output"


def run_single_asset(
    asset: str,
    data_path: Path,
    strategy: str,
    config: ExecutionConfig,
    output_dir: Path,
) -> dict:
    df = load_data(data_path)
    logger.info("Loaded %d candles for %s from %s", len(df), asset, data_path)

    bt = HistoricalBacktester(strategy=strategy, config=config)
    result = bt.run(asset, df)
    metrics = compute_metrics(result)
    regime_stats = analyze_regimes(asset, df)

    funnel = _compute_funnel(result)

    output = {
        "asset": asset,
        "strategy": strategy,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "metrics": metrics.__dict__,
        "regime": {
            "counts": regime_stats.regime_counts,
            "percentages": regime_stats.regime_pcts,
            "avg_duration": regime_stats.avg_duration,
            "transitions": regime_stats.transition_counts,
        },
        "signal_funnel": funnel,
        "trades": [_trade_to_dict(t) for t in result.trades],
    }

    print(f"\n{'=' * 60}")
    print(f"Asset: {asset} | Strategy: {strategy}")
    print(f"Period: {result.start_date} to {result.end_date}")
    print(f"{'=' * 60}")
    print(format_metrics(metrics))
    print()
    print(format_regime_report(regime_stats))
    print()
    _print_funnel(funnel)
    print()
    _print_trades(result.trades)

    out_path = output_dir / f"{asset.replace('/', '_')}_{strategy}_backtest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info("Results saved to %s", out_path)

    return output


def _compute_funnel(result) -> dict:
    total = len(result.signal_funnel)
    by_type = {}
    by_regime = {}
    for entry in result.signal_funnel:
        by_type[entry.signal_type] = by_type.get(entry.signal_type, 0) + 1
        by_regime[entry.regime] = by_regime.get(entry.regime, 0) + 1

    exit_reasons = {}
    for t in result.trades:
        reason = t.exit_reason or "UNKNOWN"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    return {
        "total_days_analyzed": total,
        "signals_by_type": by_type,
        "signals_by_regime": by_regime,
        "exit_reasons": exit_reasons,
    }


def _print_funnel(funnel: dict) -> None:
    print("Signal Funnel")
    print("-" * 40)
    print(f"  Days analyzed:     {funnel['total_days_analyzed']}")
    print("  Signals by type:")
    for sig_type, count in sorted(funnel["signals_by_type"].items()):
        print(f"    {sig_type:20s} {count:4d}")
    print("  By regime:")
    for regime, count in sorted(funnel["signals_by_regime"].items()):
        print(f"    {regime:20s} {count:4d}")
    print("  Exit reasons:")
    for reason, count in sorted(funnel["exit_reasons"].items()):
        print(f"    {reason:20s} {count:4d}")


def _print_trades(trades: list) -> None:
    if not trades:
        print("No trades executed.")
        return

    print(f"Trade Details ({len(trades)} trades)")
    print("-" * 100)
    print(f"{'#':>3} {'Asset':<8} {'Entry Date':<12} {'Exit Date':<12} {'Regime':<8} "
          f"{'Entry':>10} {'Exit':>10} {'P&L':>8} {'Reason':<15} {'MFE':>6} {'MAE':>7}")
    for t in trades:
        print(f"{t.trade_id:3d} {t.asset:<8} {t.execution_date:<12} {t.exit_date or '':.<12} "
              f"{t.regime:<8} {t.entry_price:10.2f} {t.exit_price or 0:10.2f} "
              f"{t.pnl:+8.2f} {t.exit_reason or '':<15} {t.mfe:5.1%} {t.mae:6.1%}")


def _trade_to_dict(t) -> dict:
    return {
        "trade_id": t.trade_id,
        "asset": t.asset,
        "signal_date": t.signal_date,
        "execution_date": t.execution_date,
        "regime": t.regime,
        "indicators": t.indicators,
        "entry_price": t.entry_price,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "position_size_usd": t.position_size_usd,
        "max_risk_usd": t.max_risk_usd,
        "exit_date": t.exit_date,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "pnl": t.pnl,
        "holding_days": t.holding_days,
        "mfe": t.mfe,
        "mae": t.mae,
    }


def main():
    parser = argparse.ArgumentParser(description="Run historical backtest")
    parser.add_argument("--data-dir", default="research/data")
    parser.add_argument("--strategy", default="conservative", choices=["conservative", "challenge"])
    parser.add_argument("--assets", default=None, help="Comma-separated: BTC/USD,ETH/USD")
    parser.add_argument("--output-dir", default="research/output")
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

    all_results = []
    for asset in assets:
        safe = asset.replace("/", "_")
        data_path = data_dir / f"{safe}.csv"
        if not data_path.exists():
            data_path = data_dir / f"{safe}.json"
        if not data_path.exists():
            logger.warning("No data file for %s, skipping", asset)
            continue

        result = run_single_asset(asset, data_path, args.strategy, config, output_dir)
        all_results.append(result)

    if len(all_results) > 1:
        print(f"\n{'=' * 60}")
        print("SUMMARY ACROSS ALL ASSETS")
        print(f"{'=' * 60}")
        for r in all_results:
            m = r["metrics"]
            print(f"  {r['asset']:<10} Trades: {m['num_trades']:3d}  "
                  f"WR: {m['win_rate']:5.1f}%  "
                  f"E[pnl]: ${m['expectancy']:+.2f}  "
                  f"Return: {m['total_return_pct']:+.2f}%")


if __name__ == "__main__":
    main()
