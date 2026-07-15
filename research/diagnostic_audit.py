"""Deep diagnostic audit: trace every entry filter individually per asset/period.

Outputs:
  - Per asset/period: total candles, timestamps, warmup, regime counts,
    independent filter pass rates, sequential funnel, regime overlap counts
  - Balance tracking with trade log (train period)
  - Hypothetical trade outcomes for candle-confirmation-blocked signals
  - Distinguishes "0 strategy signals" from "signals existed but execution rejected"

Usage:
    python -m research.diagnostic_audit
"""
from __future__ import annotations

import copy
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from research.schema import load_data, to_engine_df, ASSETS
from research.walk_forward import create_splits
from research.backtest_engine import HistoricalBacktester, ExecutionConfig
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, regime_nan_fields, MarketRegime
from src.strategy.engine import StrategyEngine
from src.config import settings

logging.basicConfig(level=logging.WARNING)
OUTPUT_DIR = Path("research/output")


# ---------------------------------------------------------------------------
# 1. Per-candle filter trace
# ---------------------------------------------------------------------------

def trace_filters(asset: str, df: pd.DataFrame, warmup: int = 252) -> pd.DataFrame:
    """For every tradeable candle, record each filter's pass/fail independently."""
    engine_df = to_engine_df(df)
    daily = compute_indicators(engine_df)

    rows = []
    for i in range(warmup, len(daily) - 1):
        row = daily.iloc[i]
        prev = daily.iloc[i - 1]
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

        # Has NaN in required fields?
        nan_fields = regime_nan_fields(row)
        has_valid_indicators = len(nan_fields) == 0

        f_regime_ok = regime not in (MarketRegime.PANIC, MarketRegime.DATA_INSUFFICIENT)
        f_price_above_ema200 = close > ema200 if ema200 > 0 else False
        f_candle_confirm = (prev_close > prev_ema50) if (prev_close > 0 and prev_ema50 > 0) else False
        f_spike_ok = abs(price_change_short) <= settings.vertical_spike_pct
        f_both_ema = f_price_above_ema200 and f_candle_confirm

        # Regime overlap detection: 0.30 <= er20 < 0.35 AND rvol <= rvol_pct25 AND close > ema200
        overlap_zone = (0.30 <= er20 < 0.35) and (rvol_pct25 > 0 and rvol <= rvol_pct25) and (close > ema200)

        rows.append({
            "date": date,
            "idx": i,
            "close": close,
            "ema200": ema200,
            "ema50": ema50,
            "er20": er20,
            "rvol": rvol,
            "rvol_pct25": rvol_pct25,
            "rvol_median": rvol_median,
            "adx14": adx14,
            "price_change_48h": price_change_48h,
            "price_change_short": price_change_short,
            "prev_close": prev_close,
            "prev_ema50": prev_ema50,
            "regime": regime.value,
            "has_valid_indicators": has_valid_indicators,
            "f_regime_ok": f_regime_ok,
            "f_price_above_ema200": f_price_above_ema200,
            "f_candle_confirm": f_candle_confirm,
            "f_both_ema": f_both_ema,
            "f_spike_ok": f_spike_ok,
            "overlap_zone": overlap_zone,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2. Full backtest with balance tracking and rejection logging
# ---------------------------------------------------------------------------

def run_traced_backtest(asset: str, df: pd.DataFrame, warmup: int = 252) -> dict:
    """Run the real backtest engine but capture balance at every step,
    every signal, and every rejection reason."""
    engine_df = to_engine_df(df)

    if len(engine_df) <= warmup:
        return {"error": f"Need >{warmup} candles, got {len(engine_df)}"}

    engine = StrategyEngine()
    config = ExecutionConfig()
    cash = config.starting_balance
    open_positions: list[dict] = []
    total_open_risk_usd = 0.0

    balance_log = []
    signal_log = []
    trade_log = []
    rejection_log = []
    balance_below_975_at = None
    rejected_after_975 = 0

    for i in range(warmup, len(engine_df) - 1):
        today = engine_df.iloc[i]
        tomorrow = engine_df.iloc[i + 1]
        today_date = str(today["open_time"])[:10]
        tomorrow_date = str(tomorrow["open_time"])[:10]

        history = engine_df.iloc[:i + 1].copy()
        current_price = float(today["close"])

        equity = cash + sum(p["quantity"] * current_price for p in open_positions)

        # Check challenge boundaries
        if equity <= config.loss_level or equity >= config.win_level:
            balance_log.append({
                "date": today_date, "cash": round(cash, 2),
                "equity": round(equity, 2), "open_pos": len(open_positions),
                "event": "CHALLENGE_BOUNDARY",
            })
            break

        # Process exits on tomorrow candle
        high_tom = float(tomorrow["high"])
        low_tom = float(tomorrow["low"])
        for pos in list(open_positions):
            entry = pos["entry_price"]
            sl = pos["stop_loss"]
            tp = pos["take_profit"]
            risk_per_unit = pos.get("risk_per_unit", 0)

            # Trailing stop
            breakeven_threshold = entry + risk_per_unit * 1.5 if risk_per_unit > 0 else None
            if breakeven_threshold and high_tom >= breakeven_threshold:
                sl = max(sl, entry)
                pos["stop_loss"] = sl

            sl_hit = low_tom <= sl
            tp_hit = high_tom >= tp

            exit_price = None
            exit_reason = None
            if sl_hit and tp_hit:
                exit_price = sl * (1 - config.slippage_pct)
                exit_reason = "SL_WORST_CASE"
            elif sl_hit:
                exit_price = sl * (1 - config.slippage_pct)
                exit_reason = "STOP_LOSS"
            elif tp_hit:
                exit_price = tp * (1 - config.spread_pct / 2)
                exit_reason = "TAKE_PROFIT"

            if exit_price is not None:
                entry_cost = pos["quantity"] * pos["entry_price"]
                exit_proceeds = pos["quantity"] * exit_price
                commission = (entry_cost + exit_proceeds) * config.commission_pct
                pnl = exit_proceeds - entry_cost - commission
                cash += pos["quantity"] * exit_price * (1 - config.commission_pct)
                total_open_risk_usd -= pos.get("max_loss_usd", 0)
                open_positions.remove(pos)

                trade_log.append({
                    "trade_id": pos["trade_id"],
                    "entry_date": pos["entry_date"],
                    "exit_date": tomorrow_date,
                    "entry_price": round(pos["entry_price"], 2),
                    "exit_price": round(exit_price, 2),
                    "stop_loss": round(sl, 2),
                    "take_profit": round(tp, 2),
                    "position_usd": round(pos["position_size_usd"], 2),
                    "pnl": round(pnl, 2),
                    "exit_reason": exit_reason,
                    "balance_after": round(cash + sum(p["quantity"] * current_price for p in open_positions), 2),
                })

        # Get signal
        signal = engine.analyze(
            symbol=asset,
            daily_df=history,
            h4_df=pd.DataFrame(),
            current_price=current_price,
            portfolio_balance=equity,
            open_positions=[_pos_dict(p) for p in open_positions],
            total_open_risk_usd=total_open_risk_usd,
        )

        signal_entry = {
            "date": today_date,
            "signal_type": signal.signal_type,
            "regime": signal.regime.value,
            "reason": signal.reason,
            "equity": round(equity, 2),
            "position_size_usd": round(signal.position_size_usd, 2),
        }
        signal_log.append(signal_entry)

        # Track balance guard rejections
        if equity < 975 and balance_below_975_at is None:
            balance_below_975_at = today_date

        if signal.signal_type == "BUY" and signal.position_size_usd > 0:
            exec_price = float(tomorrow["open"]) * (1 + config.spread_pct / 2 + config.slippage_pct)
            cost = signal.position_size_usd * config.commission_pct
            stop_distance_pct = (signal.entry_price - signal.stop_loss) / signal.entry_price if signal.entry_price > 0 else 0.03
            stop_loss_price = exec_price * (1 - stop_distance_pct)
            risk_per_unit = exec_price - stop_loss_price
            tp_price = exec_price + risk_per_unit * engine.take_profit_multiple

            quantity = signal.position_size_usd / exec_price
            trade_id = len(trade_log) + len(open_positions) + 1

            pos = {
                "symbol": asset,
                "entry_price": exec_price,
                "stop_loss": stop_loss_price,
                "take_profit": tp_price,
                "quantity": quantity,
                "risk_per_unit": risk_per_unit,
                "position_size_usd": signal.position_size_usd,
                "max_loss_usd": signal.max_loss_usd,
                "status": "open",
                "trade_id": trade_id,
                "entry_date": tomorrow_date,
            }
            open_positions.append(pos)
            total_open_risk_usd += signal.max_loss_usd
            cash -= (signal.position_size_usd + cost)

        elif signal.signal_type in ("SELL", "MOVE_TO_USD") and open_positions:
            for pos in list(open_positions):
                if pos.get("symbol") == asset:
                    exec_price = float(tomorrow["open"]) * (1 - config.spread_pct / 2 - config.slippage_pct)
                    entry_cost = pos["quantity"] * pos["entry_price"]
                    exit_proceeds = pos["quantity"] * exec_price
                    commission = (entry_cost + exit_proceeds) * config.commission_pct
                    pnl = exit_proceeds - entry_cost - commission
                    cash += pos["quantity"] * exec_price * (1 - config.commission_pct)
                    total_open_risk_usd -= pos.get("max_loss_usd", 0)
                    open_positions.remove(pos)
                    trade_log.append({
                        "trade_id": pos["trade_id"],
                        "entry_date": pos["entry_date"],
                        "exit_date": tomorrow_date,
                        "entry_price": round(pos["entry_price"], 2),
                        "exit_price": round(exec_price, 2),
                        "stop_loss": round(pos["stop_loss"], 2),
                        "take_profit": round(pos["take_profit"], 2),
                        "position_usd": round(pos["position_size_usd"], 2),
                        "pnl": round(pnl, 2),
                        "exit_reason": signal.signal_type,
                        "balance_after": round(cash + sum(p["quantity"] * current_price for p in open_positions), 2),
                    })

        # Log balance after NO_TRADE if balance changed due to exits
        equity_now = cash + sum(p["quantity"] * current_price for p in open_positions)

        # Track rejections after balance < 975
        if balance_below_975_at is not None and signal.signal_type == "NO_TRADE":
            # Check if this candle would have passed all other filters
            daily_computed = compute_indicators(history)
            latest = daily_computed.iloc[-1]
            prev_row = daily_computed.iloc[-2] if len(daily_computed) > 1 else latest
            regime = classify_regime(latest)
            price_ok = current_price > (float(latest.get("ema200", 0) or 0))
            candle_ok = float(prev_row.get("close", 0) or 0) > float(prev_row.get("ema50", 0) or 0)
            spike_ok = abs(float(latest.get("price_change_short", 0) or 0)) <= settings.vertical_spike_pct
            regime_ok = regime not in (MarketRegime.PANIC, MarketRegime.DATA_INSUFFICIENT)

            if regime_ok and price_ok and candle_ok and spike_ok and equity_now < 975:
                rejected_after_975 += 1
                rejection_log.append({
                    "date": today_date,
                    "equity": round(equity_now, 2),
                    "regime": regime.value,
                    "reason": "balance_below_975",
                })

        balance_log.append({
            "date": today_date, "cash": round(cash, 2),
            "equity": round(equity_now, 2), "open_pos": len(open_positions),
        })

    return {
        "trade_log": trade_log,
        "signal_log": signal_log,
        "balance_log": balance_log,
        "rejection_log": rejection_log,
        "balance_below_975_at": balance_below_975_at,
        "rejected_after_975": rejected_after_975,
        "final_equity": round(cash + sum(p["quantity"] * current_price for p in open_positions), 2) if 'current_price' in dir() else round(cash, 2),
    }


def _pos_dict(pos: dict) -> dict:
    return {
        "symbol": pos["symbol"],
        "entry_price": pos["entry_price"],
        "stop_loss": pos["stop_loss"],
        "risk_per_unit": pos["risk_per_unit"],
        "status": "open",
    }


# ---------------------------------------------------------------------------
# 3. Hypothetical outcomes for candle-confirmation-blocked signals
# ---------------------------------------------------------------------------

def hypothetical_without_candle_confirm(asset: str, df: pd.DataFrame, warmup: int = 252) -> list[dict]:
    """Find candles where all filters pass EXCEPT candle confirmation,
    and simulate what would have happened if we entered at T+1 open."""
    engine_df = to_engine_df(df)
    daily = compute_indicators(engine_df)
    config = ExecutionConfig()
    engine = StrategyEngine()

    hypotheticals = []

    for i in range(warmup, len(daily) - 1):
        row = daily.iloc[i]
        prev = daily.iloc[i - 1]
        close = float(row["close"])
        ema200 = float(row.get("ema200", 0) or 0)
        er20 = float(row.get("er20", 0) or 0)
        prev_close = float(prev.get("close", 0) or 0)
        prev_ema50 = float(prev.get("ema50", 0) or 0)
        price_change_short = float(row.get("price_change_short", 0) or 0)

        regime = classify_regime(row)
        regime_ok = regime not in (MarketRegime.PANIC, MarketRegime.DATA_INSUFFICIENT)
        price_ok = close > ema200 if ema200 > 0 else False
        candle_ok = (prev_close > prev_ema50) if (prev_close > 0 and prev_ema50 > 0) else False
        spike_ok = abs(price_change_short) <= settings.vertical_spike_pct

        # All pass EXCEPT candle confirmation
        if regime_ok and price_ok and spike_ok and not candle_ok:
            date = str(row.get("open_time", ""))[:10]
            tomorrow = daily.iloc[i + 1]
            exec_price = float(tomorrow["open"]) * (1 + config.spread_pct / 2 + config.slippage_pct)

            # Determine stop and TP
            if regime == MarketRegime.TREND:
                stop_dist = 0.03
            elif regime == MarketRegime.CHOP:
                stop_dist = 0.025
            elif regime == MarketRegime.LOWVOL:
                stop_dist = 0.02
            else:
                continue
            sl_price = exec_price * (1 - stop_dist)
            risk_per_unit = exec_price - sl_price
            tp_price = exec_price + risk_per_unit * engine.take_profit_multiple

            # Simulate forward: check next N candles for SL/TP hit
            outcome = "OPEN"
            exit_price = None
            exit_date = None
            exit_day = 0
            max_favorable = 0.0
            max_adverse = 0.0
            current_sl = sl_price

            for j in range(i + 1, min(i + 31, len(daily))):
                candle = daily.iloc[j]
                h = float(candle["high"])
                l = float(candle["low"])
                c_date = str(candle.get("open_time", ""))[:10]

                max_favorable = max(max_favorable, (h - exec_price) / exec_price)
                max_adverse = min(max_adverse, (l - exec_price) / exec_price)

                # Trailing stop
                be_threshold = exec_price + risk_per_unit * 1.5
                if h >= be_threshold:
                    current_sl = max(current_sl, exec_price)

                sl_hit = l <= current_sl
                tp_hit = h >= tp_price

                if sl_hit and tp_hit:
                    outcome = "SL_WORST_CASE"
                    exit_price = current_sl * (1 - config.slippage_pct)
                    exit_date = c_date
                    exit_day = j - i
                    break
                elif sl_hit:
                    outcome = "STOP_LOSS" if current_sl < exec_price else "BREAKEVEN_STOP"
                    exit_price = current_sl * (1 - config.slippage_pct)
                    exit_date = c_date
                    exit_day = j - i
                    break
                elif tp_hit:
                    outcome = "TAKE_PROFIT"
                    exit_price = tp_price * (1 - config.spread_pct / 2)
                    exit_date = c_date
                    exit_day = j - i
                    break

            pnl = 0.0
            if exit_price is not None:
                pos_size = 30.0  # Approximate $30 position
                qty = pos_size / exec_price
                entry_cost = qty * exec_price
                exit_proceeds = qty * exit_price
                commission = (entry_cost + exit_proceeds) * config.commission_pct
                pnl = exit_proceeds - entry_cost - commission

            hypotheticals.append({
                "signal_date": date,
                "regime": regime.value,
                "er20": round(er20, 3),
                "entry_price": round(exec_price, 2),
                "stop_loss": round(sl_price, 2),
                "take_profit": round(tp_price, 2),
                "outcome": outcome,
                "exit_price": round(exit_price, 2) if exit_price else None,
                "exit_date": exit_date,
                "holding_days": exit_day,
                "pnl_approx": round(pnl, 2),
                "mfe_pct": round(max_favorable * 100, 2),
                "mae_pct": round(max_adverse * 100, 2),
                "prev_close": round(prev_close, 2),
                "prev_ema50": round(prev_ema50, 2),
                "gap_to_ema50": round((prev_close - prev_ema50) / prev_ema50 * 100, 2) if prev_ema50 > 0 else 0,
            })

    return hypotheticals


# ---------------------------------------------------------------------------
# 4. Output formatting
# ---------------------------------------------------------------------------

def print_asset_period(asset: str, period_name: str, trace_df: pd.DataFrame,
                       first_ts: str, last_ts: str, total_candles: int):
    n = len(trace_df)
    print(f"\n{'='*70}")
    print(f"  {asset} — {period_name}")
    print(f"{'='*70}")
    print(f"  total candles:            {total_candles}")
    print(f"  first timestamp:          {first_ts}")
    print(f"  last timestamp:           {last_ts}")
    print(f"  candles after warm-up:    {n}")

    if n == 0:
        print("  (no tradeable candles)")
        return

    valid = trace_df["has_valid_indicators"].sum()
    print(f"  candles after dropna:     {valid}")

    # Regime counts
    print(f"\n  regime counts:")
    for r in ["TREND", "CHOP", "LOWVOL", "PANIC", "DATA_INSUFFICIENT"]:
        cnt = (trace_df["regime"] == r).sum()
        pct = cnt / n * 100
        print(f"    {r:22s}: {cnt:4d} ({pct:5.1f}%)")

    # Independent filter pass rates
    print(f"\n  independent filter pass rates:")
    print(f"    price > EMA200:         {trace_df['f_price_above_ema200'].sum():4d}/{n} ({trace_df['f_price_above_ema200'].mean()*100:5.1f}%)")
    print(f"    prev close > prev EMA50:{trace_df['f_candle_confirm'].sum():4d}/{n} ({trace_df['f_candle_confirm'].mean()*100:5.1f}%)")
    print(f"    both EMA conditions:    {trace_df['f_both_ema'].sum():4d}/{n} ({trace_df['f_both_ema'].mean()*100:5.1f}%)")
    print(f"    vertical spike passed:  {trace_df['f_spike_ok'].sum():4d}/{n} ({trace_df['f_spike_ok'].mean()*100:5.1f}%)")

    # Sequential funnel
    print(f"\n  sequential funnel:")
    remaining = n
    print(f"    Starting candles:           {remaining}")

    mask = trace_df["has_valid_indicators"]
    print(f"    → after valid indicators:   {mask.sum():4d} (rejected {remaining - mask.sum()})")

    mask2 = mask & trace_df["f_regime_ok"]
    print(f"    → after allowed regime:     {mask2.sum():4d} (rejected {mask.sum() - mask2.sum()})")

    mask3 = mask2 & trace_df["f_price_above_ema200"]
    print(f"    → after EMA200:             {mask3.sum():4d} (rejected {mask2.sum() - mask3.sum()})")

    mask4 = mask3 & trace_df["f_candle_confirm"]
    print(f"    → after candle confirm:     {mask4.sum():4d} (rejected {mask3.sum() - mask4.sum()})")

    mask5 = mask4 & trace_df["f_spike_ok"]
    print(f"    → after spike filter:       {mask5.sum():4d} (rejected {mask4.sum() - mask5.sum()})")

    print(f"    → pre-portfolio signals:    {mask5.sum():4d}")
    print(f"    (portfolio restrictions and balance guards applied at runtime by engine)")

    # Regime overlap
    overlap_cnt = trace_df["overlap_zone"].sum()
    print(f"\n  regime overlap (0.30<=er20<0.35, rvol<=pct25, close>ema200): {overlap_cnt}")
    if overlap_cnt > 0:
        overlap_rows = trace_df[trace_df["overlap_zone"]]
        print(f"    dates: {', '.join(overlap_rows['date'].tolist()[:20])}")
        if len(overlap_rows) > 20:
            print(f"    ... and {len(overlap_rows) - 20} more")


def print_balance_analysis(bt_result: dict, period_name: str):
    print(f"\n  --- BALANCE TRACKING ({period_name}) ---")
    trades = bt_result["trade_log"]
    if not trades:
        print("    No trades executed")
        print(f"    Balance stayed at starting value")
        if bt_result["balance_below_975_at"]:
            print(f"    *** Balance dropped below $975 at: {bt_result['balance_below_975_at']}")
        return

    print(f"    Trade log:")
    for t in trades:
        print(f"      #{t['trade_id']}: {t['entry_date']}→{t['exit_date']} "
              f"entry=${t['entry_price']} exit=${t['exit_price']} "
              f"size=${t['position_usd']} pnl=${t['pnl']:+.2f} "
              f"reason={t['exit_reason']} balance_after=${t['balance_after']}")

    if bt_result["balance_below_975_at"]:
        print(f"\n    *** Balance first dropped below $975 at: {bt_result['balance_below_975_at']}")
        print(f"    *** Valid signals rejected after that: {bt_result['rejected_after_975']}")
        if bt_result["rejection_log"]:
            print(f"    Rejection details:")
            for r in bt_result["rejection_log"]:
                print(f"      {r['date']}: equity=${r['equity']} regime={r['regime']}")
    else:
        print(f"\n    Balance never dropped below $975")

    # Show balance before every signal
    buy_signals = [s for s in bt_result["signal_log"] if s["signal_type"] == "BUY"]
    if buy_signals:
        print(f"\n    Balance before every BUY signal:")
        for s in buy_signals:
            print(f"      {s['date']}: equity=${s['equity']} regime={s['regime']} size=${s['position_size_usd']}")

    # Distinguish 0 signals vs rejected signals
    total_signals = len(bt_result["signal_log"])
    no_trade_count = sum(1 for s in bt_result["signal_log"] if s["signal_type"] == "NO_TRADE")
    buy_count = sum(1 for s in bt_result["signal_log"] if s["signal_type"] == "BUY")
    sell_count = sum(1 for s in bt_result["signal_log"] if s["signal_type"] in ("SELL", "MOVE_TO_USD"))
    tp_count = sum(1 for s in bt_result["signal_log"] if s["signal_type"] == "TAKE_PROFIT")

    print(f"\n    Signal summary: {total_signals} total candles analyzed")
    print(f"      BUY:         {buy_count}")
    print(f"      SELL/MOVE:   {sell_count}")
    print(f"      TAKE_PROFIT: {tp_count}")
    print(f"      NO_TRADE:    {no_trade_count}")

    if buy_count == 0:
        # Check if any signals existed but were rejected by portfolio
        reasons = {}
        for s in bt_result["signal_log"]:
            if s["signal_type"] == "NO_TRADE":
                r = s["reason"]
                reasons[r] = reasons.get(r, 0) + 1
        print(f"\n    NO_TRADE reason breakdown:")
        for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"      {cnt:4d}: {reason}")


def print_hypotheticals(hyps: list[dict], period_name: str):
    if not hyps:
        print(f"\n  --- HYPOTHETICALS WITHOUT CANDLE CONFIRM ({period_name}): none ---")
        return

    print(f"\n  --- HYPOTHETICALS WITHOUT CANDLE CONFIRM ({period_name}) ---")
    print(f"    Candles blocked by candle confirm but passing all other filters: {len(hyps)}")
    print(f"    (simulated with $30 position, trailing stop at +1.5R→breakeven)")

    wins = sum(1 for h in hyps if h["outcome"] == "TAKE_PROFIT")
    losses = sum(1 for h in hyps if h["outcome"] in ("STOP_LOSS", "SL_WORST_CASE"))
    breakevens = sum(1 for h in hyps if h["outcome"] == "BREAKEVEN_STOP")
    still_open = sum(1 for h in hyps if h["outcome"] == "OPEN")

    print(f"    Outcomes: {wins} TP, {losses} SL, {breakevens} BE, {still_open} still open (30d)")
    total_pnl = sum(h["pnl_approx"] for h in hyps if h["pnl_approx"] != 0)
    print(f"    Total hypothetical PnL: ${total_pnl:+.2f}")

    print(f"\n    Detail:")
    for h in hyps:
        print(f"      {h['signal_date']}: regime={h['regime']} er20={h['er20']} "
              f"entry=${h['entry_price']} → {h['outcome']} "
              f"pnl=${h['pnl_approx']:+.2f} hold={h['holding_days']}d "
              f"mfe={h['mfe_pct']:+.1f}% mae={h['mae_pct']:+.1f}% "
              f"gap_to_ema50={h['gap_to_ema50']:+.1f}%")


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def main():
    data_dir = Path("research/data")
    if not data_dir.exists():
        print("ERROR: research/data directory not found.")
        print("Run from project root after fetching data:")
        print("  python -m research.fetch_data --provider kraken --days 730")
        sys.exit(1)

    data_files = sorted(data_dir.glob("*.csv")) + sorted(data_dir.glob("*.json"))
    data_files = [f for f in data_files if f.name != ".gitkeep"]
    if not data_files:
        print("ERROR: No data files found in research/data/")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for asset_name in ASSETS:
        safe_name = asset_name.replace("/", "_")
        candidates = [f for f in data_files if safe_name in f.stem or safe_name.lower() in f.stem.lower()]
        if not candidates:
            print(f"\n{'#'*70}")
            print(f"ASSET: {asset_name} — NO DATA FILE FOUND, SKIPPING")
            continue

        data_path = candidates[0]
        df = load_data(data_path)
        asset_df = df[df["asset"] == asset_name].copy()
        if asset_df.empty:
            asset_df = df.copy()

        total_candles = len(asset_df)
        first_ts = str(asset_df["timestamp"].iloc[0])[:10] if total_candles > 0 else "N/A"
        last_ts = str(asset_df["timestamp"].iloc[-1])[:10] if total_candles > 0 else "N/A"

        print(f"\n{'#'*70}")
        print(f"# ASSET: {asset_name}")
        print(f"# Data file: {data_path.name}")
        print(f"# Total candles: {total_candles}, {first_ts} to {last_ts}")
        print(f"{'#'*70}")

        # Create splits
        try:
            splits = create_splits(asset_df, warmup_candles=252)
        except ValueError as e:
            print(f"  Cannot create splits: {e}")
            continue

        # Print split boundaries
        print(f"\n  Walk-forward splits:")
        for s in splits:
            print(f"    {s.name:12s}: idx [{s.start_idx}:{s.end_idx}] = {s.end_idx - s.start_idx} candles, "
                  f"{s.start_date} to {s.end_date}")

        # FULL dataset trace
        trace_full = trace_filters(asset_name, asset_df)
        print_asset_period(asset_name, "FULL DATASET", trace_full, first_ts, last_ts, total_candles)

        # Per-split trace
        for split in splits:
            split_df = asset_df.iloc[split.start_idx:split.end_idx].copy().reset_index(drop=True)
            split_total = len(split_df)
            split_first = str(split_df["timestamp"].iloc[0])[:10] if split_total > 0 else "N/A"
            split_last = str(split_df["timestamp"].iloc[-1])[:10] if split_total > 0 else "N/A"

            trace_split = trace_filters(asset_name, split_df)
            print_asset_period(asset_name, f"{split.name} ({split.start_date} to {split.end_date})",
                              trace_split, split_first, split_last, split_total)

            # Run traced backtest for this split
            bt_result = run_traced_backtest(asset_name, split_df)
            if "error" not in bt_result:
                print_balance_analysis(bt_result, split.name)

            # Hypotheticals only for train and validation (NOT test)
            if split.name in ("train", "validation"):
                hyps = hypothetical_without_candle_confirm(asset_name, split_df)
                print_hypotheticals(hyps, split.name)

    print(f"\n{'#'*70}")
    print("# DIAGNOSTIC AUDIT COMPLETE")
    print(f"{'#'*70}")


if __name__ == "__main__":
    main()
