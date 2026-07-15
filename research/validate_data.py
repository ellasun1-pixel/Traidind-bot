"""Validate OHLCV data before backtesting.

Usage:
    python -m research.validate_data [--data-dir research/data]
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from research.schema import CANONICAL_COLUMNS, load_data

logger = logging.getLogger(__name__)

MIN_WARMUP_CANDLES = 252


class ValidationReport:
    def __init__(self, asset: str):
        self.asset = asset
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: dict[str, object] = {}

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "passed": self.passed,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
        }

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"[{status}] {self.asset}"]
        for k, v in self.info.items():
            lines.append(f"  {k}: {v}")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN: {w}")
        return "\n".join(lines)


def validate(df: pd.DataFrame) -> ValidationReport:
    if df.empty:
        report = ValidationReport("EMPTY")
        report.errors.append("DataFrame is empty")
        return report

    asset = df["asset"].iloc[0]
    report = ValidationReport(asset)

    _check_columns(df, report)
    _check_timestamps(df, report)
    _check_chronology(df, report)
    _check_duplicates(df, report)
    _check_no_future(df, report)
    _check_ohlc_values(df, report)
    _check_ohlc_relationships(df, report)
    _check_gaps(df, report)
    _check_warmup(df, report)
    _report_date_range(df, report)

    return report


def _check_columns(df: pd.DataFrame, report: ValidationReport) -> None:
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        report.errors.append(f"Missing columns: {missing}")


def _check_timestamps(df: pd.DataFrame, report: ValidationReport) -> None:
    if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
        report.errors.append("timestamp column is not datetime type")
        return

    non_utc = df[df["timestamp"].dt.tz is None] if df["timestamp"].dt.tz is None else pd.DataFrame()
    tz = df["timestamp"].dt.tz
    if tz is None:
        report.errors.append("Timestamps are not timezone-aware (must be UTC)")
    elif str(tz) != "UTC":
        report.errors.append(f"Timestamps are in {tz}, must be UTC")

    hours = df["timestamp"].dt.hour
    non_midnight = (hours != 0).sum()
    if non_midnight > 0:
        midnight_pct = (1 - non_midnight / len(df)) * 100
        if midnight_pct < 50:
            report.warnings.append(
                f"{non_midnight}/{len(df)} candles not at midnight — may not be daily"
            )


def _check_chronology(df: pd.DataFrame, report: ValidationReport) -> None:
    diffs = df["timestamp"].diff().dropna()
    negative = (diffs < timedelta(0)).sum()
    if negative > 0:
        report.errors.append(f"{negative} timestamps are out of chronological order")


def _check_duplicates(df: pd.DataFrame, report: ValidationReport) -> None:
    dupes = df["timestamp"].duplicated().sum()
    if dupes > 0:
        report.errors.append(f"{dupes} duplicate timestamps found")


def _check_no_future(df: pd.DataFrame, report: ValidationReport) -> None:
    now = pd.Timestamp.now(tz="UTC")
    future = (df["timestamp"] > now).sum()
    if future > 0:
        report.errors.append(f"{future} candles have timestamps in the future")


def _check_ohlc_values(df: pd.DataFrame, report: ValidationReport) -> None:
    for col in ["open", "high", "low", "close"]:
        nulls = df[col].isna().sum()
        if nulls > 0:
            report.errors.append(f"{nulls} missing values in {col}")

        neg = (df[col] <= 0).sum()
        if neg > 0:
            report.errors.append(f"{neg} non-positive values in {col}")

        infs = df[col].apply(lambda x: math.isinf(x) if not pd.isna(x) else False).sum()
        if infs > 0:
            report.errors.append(f"{infs} infinite values in {col}")

    vol_nulls = df["volume"].isna().sum()
    if vol_nulls > 0:
        report.warnings.append(f"{vol_nulls} missing volume values")


def _check_ohlc_relationships(df: pd.DataFrame, report: ValidationReport) -> None:
    bad_hl = (df["high"] < df["low"]).sum()
    if bad_hl > 0:
        report.errors.append(f"{bad_hl} candles where high < low")

    bad_oh = (df["open"] > df["high"]).sum()
    bad_ol = (df["open"] < df["low"]).sum()
    if bad_oh > 0:
        report.errors.append(f"{bad_oh} candles where open > high")
    if bad_ol > 0:
        report.errors.append(f"{bad_ol} candles where open < low")

    bad_ch = (df["close"] > df["high"]).sum()
    bad_cl = (df["close"] < df["low"]).sum()
    if bad_ch > 0:
        report.errors.append(f"{bad_ch} candles where close > high")
    if bad_cl > 0:
        report.errors.append(f"{bad_cl} candles where close < low")


def _check_gaps(df: pd.DataFrame, report: ValidationReport) -> None:
    if len(df) < 2:
        return

    diffs = df["timestamp"].diff().dropna()
    median_diff = diffs.median()
    threshold = median_diff * 2.5

    gaps = diffs[diffs > threshold]
    if len(gaps) > 0:
        gap_dates = df["timestamp"].iloc[gaps.index].tolist()
        report.warnings.append(f"{len(gaps)} gaps detected (>{threshold.days}d threshold)")
        report.info["gap_dates"] = [str(d.date()) for d in gap_dates[:10]]
        if len(gaps) > 10:
            report.info["gap_dates_truncated"] = True


def _check_warmup(df: pd.DataFrame, report: ValidationReport) -> None:
    n = len(df)
    report.info["total_candles"] = n
    if n < MIN_WARMUP_CANDLES:
        report.errors.append(
            f"Only {n} candles, need at least {MIN_WARMUP_CANDLES} for indicator warm-up"
        )


def _report_date_range(df: pd.DataFrame, report: ValidationReport) -> None:
    if df.empty:
        return
    report.info["first_candle"] = str(df["timestamp"].iloc[0].date())
    report.info["last_candle"] = str(df["timestamp"].iloc[-1].date())
    span = df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]
    report.info["span_days"] = span.days


def validate_file(path: Path) -> ValidationReport:
    df = load_data(path)
    return validate(df)


def main():
    parser = argparse.ArgumentParser(description="Validate OHLCV data files")
    parser.add_argument("--data-dir", default="research/data", help="Directory with data files")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"Data directory {data_dir} does not exist")
        sys.exit(1)

    files = sorted(data_dir.glob("*.csv")) + sorted(data_dir.glob("*.json"))
    files = [f for f in files if f.name != ".gitkeep"]

    if not files:
        print(f"No CSV or JSON files found in {data_dir}")
        sys.exit(1)

    all_passed = True
    for path in files:
        report = validate_file(path)
        print(report)
        print()
        if not report.passed:
            all_passed = False

    if all_passed:
        print("All files passed validation.")
    else:
        print("Some files failed validation. Fix errors before backtesting.")
        sys.exit(1)


if __name__ == "__main__":
    main()
