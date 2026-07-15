"""Generate comprehensive research report.

Usage:
    python -m research.generate_report [--output-dir research/output]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ASSET_COMBOS = {
    "BTC_only": ["BTC/USD"],
    "BTC_ETH": ["BTC/USD", "ETH/USD"],
    "BTC_ETH_LINK": ["BTC/USD", "ETH/USD", "LINK/USD"],
    "all_five": ["BTC/USD", "ETH/USD", "XRP/USD", "LINK/USD", "LTC/USD"],
}


def main():
    parser = argparse.ArgumentParser(description="Generate research report")
    parser.add_argument("--output-dir", default="research/output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    output_dir = Path(args.output_dir)

    json_files = sorted(output_dir.glob("*_backtest.json")) + sorted(output_dir.glob("*_walkforward.json"))
    if not json_files:
        print("No result files found. Run backtest and walk-forward first.")
        sys.exit(1)

    report_lines = [
        "=" * 70,
        "PHASE 2 — HISTORICAL MARKET VALIDATION REPORT",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 70,
        "",
    ]

    backtest_results = {}
    wf_results = {}

    for path in json_files:
        with open(path) as f:
            data = json.load(f)

        if "_backtest.json" in path.name:
            asset = data.get("asset", path.stem)
            backtest_results[asset] = data
        elif "_walkforward.json" in path.name:
            asset = data.get("asset", path.stem)
            wf_results[asset] = data

    report_lines.append("SECTION 1: PER-ASSET BACKTEST RESULTS")
    report_lines.append("-" * 50)
    for asset, data in sorted(backtest_results.items()):
        m = data.get("metrics", {})
        report_lines.append(f"\n{asset} ({data.get('strategy', '?')}):")
        report_lines.append(f"  Period: {data.get('start_date')} to {data.get('end_date')}")
        report_lines.append(f"  Trades: {m.get('num_trades', 0)}")
        report_lines.append(f"  Win rate: {m.get('win_rate', 0):.1f}%")
        report_lines.append(f"  Expectancy: ${m.get('expectancy', 0):.2f}")
        report_lines.append(f"  Profit factor: {m.get('profit_factor', 0):.2f}")
        report_lines.append(f"  Total return: {m.get('total_return_pct', 0):+.2f}%")
        report_lines.append(f"  Max drawdown: {m.get('max_drawdown_pct', 0):.2f}%")
        report_lines.append(f"  Sharpe: {m.get('sharpe_ratio', 0):.2f}")

        regime = data.get("regime", {})
        if regime:
            report_lines.append(f"  Regime distribution: {regime.get('percentages', {})}")

        funnel = data.get("signal_funnel", {})
        if funnel:
            report_lines.append(f"  Signal funnel: {funnel.get('signals_by_type', {})}")
            report_lines.append(f"  Exit reasons: {funnel.get('exit_reasons', {})}")

    report_lines.append(f"\n\nSECTION 2: WALK-FORWARD RESULTS")
    report_lines.append("-" * 50)
    for asset, data in sorted(wf_results.items()):
        report_lines.append(f"\n{asset} ({data.get('strategy', '?')}, {data.get('mode', '?')}):")
        for split in data.get("splits", []):
            m = split.get("metrics", {})
            if not m:
                continue
            report_lines.append(
                f"  {split['name']:12s} {split.get('start_date', '?')} to {split.get('end_date', '?')}  "
                f"Trades: {m.get('num_trades', 0):3d}  WR: {m.get('win_rate', 0):5.1f}%  "
                f"E: ${m.get('expectancy', 0):+.2f}  PF: {m.get('profit_factor', 0):.2f}  "
                f"Ret: {m.get('total_return_pct', 0):+.2f}%"
            )

    report_lines.append(f"\n\nSECTION 3: ASSET UNIVERSE COMPARISON")
    report_lines.append("-" * 50)
    for combo_name, combo_assets in ASSET_COMBOS.items():
        available = [a for a in combo_assets if a in backtest_results]
        if not available:
            continue
        total_trades = sum(backtest_results[a]["metrics"].get("num_trades", 0) for a in available)
        total_pnl = sum(backtest_results[a]["metrics"].get("total_return_usd", 0) for a in available)
        report_lines.append(f"  {combo_name}: {len(available)} assets, {total_trades} trades, ${total_pnl:+.2f}")

    report_lines.append(f"\n\nSECTION 4: DECISION CRITERIA")
    report_lines.append("-" * 50)
    report_lines.append("Criteria for production modification:")
    report_lines.append("  1. Positive OOS expectancy on real data")
    report_lines.append("  2. Profit factor > 1.0")
    report_lines.append("  3. Max drawdown < 5%")
    report_lines.append("  4. Sufficient trade count for statistical significance")
    report_lines.append("  5. Challenge pass rate > 0%")

    report_text = "\n".join(report_lines)
    print(report_text)

    report_path = output_dir / "phase2_report.txt"
    with open(report_path, "w") as f:
        f.write(report_text)
    logger.info("Report saved to %s", report_path)


if __name__ == "__main__":
    main()
