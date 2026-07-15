"""
Controlled Strategy Research Study
===================================
Sections 1-10 as specified in the research protocol.

IMPORTANT: This study uses SYNTHETIC market data generated via geometric
Brownian motion. No real historical data is available in this environment.
All conclusions must be validated against real historical data before
any production changes.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from src.strategy.backtester import STARTING_BALANCE, WIN_LEVEL, LOSS_LEVEL


# ---------------------------------------------------------------------------
# Data generation
# ---------------------------------------------------------------------------

def make_market(n, base, trend, vol, seed):
    rng = np.random.RandomState(seed)
    returns = rng.normal(trend, vol, n)
    closes = base * np.cumprod(1 + returns)
    highs = closes * (1 + rng.uniform(0.002, 0.02, n))
    lows = closes * (1 - rng.uniform(0.002, 0.02, n))
    opens = np.roll(closes, 1); opens[0] = base
    dates = pd.date_range("2024-01-01", periods=n, freq="1D")
    return pd.DataFrame({
        "open_time": dates, "open": opens, "high": highs,
        "low": lows, "close": closes, "volume": rng.uniform(500, 2000, n),
    })


ASSETS = {
    "BTC/USD": {"base": 50000, "trend": 0.0008, "vol": 0.022, "seed": 42},
    "ETH/USD": {"base": 3000,  "trend": 0.0006, "vol": 0.028, "seed": 7},
    "XRP/USD": {"base": 0.60,  "trend": 0.0003, "vol": 0.032, "seed": 99},
    "LINK/USD": {"base": 15,   "trend": 0.0005, "vol": 0.030, "seed": 13},
    "LTC/USD": {"base": 80,    "trend": 0.0004, "vol": 0.026, "seed": 55},
}

N_CANDLES = 600
COMMISSION = 0.0026
SPREAD = 0.001


# ---------------------------------------------------------------------------
# Fast backtester — pre-computes indicators once
# ---------------------------------------------------------------------------

@dataclass
class DetailedTrade:
    trade_id: int
    asset: str
    entry_day: int
    exit_day: int
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    position_value: float
    risk_dollars: float
    exit_reason: str
    pnl: float
    return_pct: float
    regime_at_entry: str
    ema50_at_entry: float
    ema200_at_entry: float
    er20_at_entry: float
    adx14_at_entry: float
    rvol_at_entry: float
    mfe: float
    mae: float
    mfe_pct: float
    mae_pct: float
    candles_held: int
    price_recovered: bool
    close_recovered: bool
    stop_distance_pct: float
    atr_at_entry: float
    stop_as_atr: float
    regime_after: str
    reached_1r: bool
    reached_1_5r: bool
    reached_2r: bool
    reached_3r: bool
    days_to_1r: int
    days_to_1_5r: int
    days_to_2r: int
    days_to_3r: int


def compute_atr(enriched, idx, period=14):
    start = max(0, idx - period + 1)
    w = enriched.iloc[start:idx + 1]
    if len(w) < 2:
        return 0.0
    trs = []
    for i in range(1, len(w)):
        h, l, pc = float(w.iloc[i]["high"]), float(w.iloc[i]["low"]), float(w.iloc[i-1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(trs))


def excursions(enriched, entry_price, entry_day, exit_day, stop_loss, risk_per_unit):
    sl = enriched.iloc[entry_day:exit_day + 1]
    if len(sl) == 0:
        return 0, 0, {}, {}, False, False

    highs = sl["high"].values.astype(float)
    lows = sl["low"].values.astype(float)

    mfe = float(np.max(highs) - entry_price)
    mae = float(entry_price - np.min(lows))

    reached, days_to = {}, {}
    for label, mult in [("1r", 1.0), ("1.5r", 1.5), ("2r", 2.0), ("3r", 3.0)]:
        target = entry_price + risk_per_unit * mult
        hits = np.where(highs >= target)[0]
        reached[label] = len(hits) > 0
        days_to[label] = int(hits[0]) if len(hits) > 0 else -1

    recovered = close_recovered = False
    if exit_day < len(enriched) - 5:
        future = enriched.iloc[exit_day:min(exit_day + 20, len(enriched))]
        if len(future) > 0:
            recovered = bool(np.any(future["high"].values.astype(float) >= entry_price))
            close_recovered = bool(np.any(future["close"].values.astype(float) >= entry_price))

    return mfe, mae, reached, days_to, recovered, close_recovered


def fast_backtest(
    enriched: pd.DataFrame,
    symbol: str,
    *,
    starting_balance: float = STARTING_BALANCE,
    tp_multiple: float = 3.0,
    balance_guard_mode: str = "original",
    regime_filter: set[str] | None = None,
    entry_mode: str = "conservative",
) -> dict:
    """
    Fast backtester that works on pre-enriched data.
    entry_mode: "conservative" uses production rules, "early_trend" uses modified rules.
    """
    balance = starting_balance
    peak = balance
    max_dd = 0.0
    open_positions = []
    trades = []
    equity = [balance]
    passed = failed = False
    tc = 0
    lookback = 200

    for day_idx in range(lookback, len(enriched)):
        today = enriched.iloc[day_idx]
        price = float(today["close"])
        high = float(today["high"])
        low = float(today["low"])
        day_date = str(today.get("open_time", f"day_{day_idx}"))[:10]

        # Close positions on stops/TPs
        still_open = []
        for pos in open_positions:
            stop = pos["stop_loss"]
            tp = pos["take_profit"]

            hit_stop = low <= stop
            hit_tp = tp > 0 and high >= tp

            if hit_stop:
                exit_p = stop
                reason = "stop_loss"
            elif hit_tp:
                exit_p = tp
                reason = "take_profit"
            else:
                still_open.append(pos)
                continue

            cost = pos["pv"] * (COMMISSION + SPREAD)
            pnl = pos["pv"] * (exit_p - pos["ep"]) / pos["ep"] - cost
            balance += pnl

            mfe, mae, reached, dtr, rec, crec = excursions(
                enriched, pos["ep"], pos["ed"], day_idx, stop, pos["rpu"])
            atr = pos["atr"]
            sd = pos["ep"] - stop
            ra = str(classify_regime(enriched.iloc[min(day_idx + 1, len(enriched) - 1)]))

            tc += 1
            trades.append(DetailedTrade(
                trade_id=tc, asset=symbol,
                entry_day=pos["ed"], exit_day=day_idx,
                entry_date=pos["edate"], exit_date=day_date,
                entry_price=pos["ep"], exit_price=exit_p,
                stop_loss=stop, take_profit=tp,
                position_value=pos["pv"], risk_dollars=pos["rd"],
                exit_reason=reason, pnl=pnl,
                return_pct=pnl / pos["pv"] if pos["pv"] > 0 else 0,
                regime_at_entry=pos["regime"],
                ema50_at_entry=pos["ema50"], ema200_at_entry=pos["ema200"],
                er20_at_entry=pos["er20"], adx14_at_entry=pos["adx14"],
                rvol_at_entry=pos["rvol"],
                mfe=mfe, mae=mae,
                mfe_pct=mfe / pos["ep"] if pos["ep"] > 0 else 0,
                mae_pct=mae / pos["ep"] if pos["ep"] > 0 else 0,
                candles_held=day_idx - pos["ed"],
                price_recovered=rec, close_recovered=crec,
                stop_distance_pct=sd / pos["ep"] if pos["ep"] > 0 else 0,
                atr_at_entry=atr,
                stop_as_atr=sd / atr if atr > 0 else 0,
                regime_after=ra,
                reached_1r=reached.get("1r", False),
                reached_1_5r=reached.get("1.5r", False),
                reached_2r=reached.get("2r", False),
                reached_3r=reached.get("3r", False),
                days_to_1r=dtr.get("1r", -1),
                days_to_1_5r=dtr.get("1.5r", -1),
                days_to_2r=dtr.get("2r", -1),
                days_to_3r=dtr.get("3r", -1),
            ))

        open_positions = still_open

        if balance <= LOSS_LEVEL:
            failed = True; break
        if balance >= WIN_LEVEL:
            passed = True; break

        # Balance guard
        if balance_guard_mode == "graduated":
            if balance < 965:
                equity.append(balance)
                peak = max(peak, balance)
                if peak > 0: max_dd = max(max_dd, (peak - balance) / peak)
                continue
            elif balance < 975:
                risk_scale = 0.25
            elif balance < 985:
                risk_scale = 0.50
            else:
                risk_scale = 1.0
        else:
            risk_scale = 1.0

        # Signal generation (inlined, no compute_indicators call)
        regime = classify_regime(today)

        if regime_filter and regime.value in regime_filter:
            equity.append(balance)
            peak = max(peak, balance)
            if peak > 0: max_dd = max(max_dd, (peak - balance) / peak)
            continue

        if regime == MarketRegime.PANIC or regime == MarketRegime.DATA_INSUFFICIENT:
            equity.append(balance)
            peak = max(peak, balance)
            if peak > 0: max_dd = max(max_dd, (peak - balance) / peak)
            continue

        # Sell conditions (check existing positions)
        if open_positions:
            for pos in open_positions:
                if pos["stop_loss"] and price <= pos["stop_loss"]:
                    pass  # already handled above
            if balance < 975:
                for pos in open_positions:
                    cost = pos["pv"] * (COMMISSION + SPREAD)
                    pnl = pos["pv"] * (price - pos["ep"]) / pos["ep"] - cost
                    balance += pnl
                    tc += 1
                    mfe, mae, reached, dtr, rec, crec = excursions(
                        enriched, pos["ep"], pos["ed"], day_idx, pos["stop_loss"], pos["rpu"])
                    trades.append(DetailedTrade(
                        trade_id=tc, asset=symbol,
                        entry_day=pos["ed"], exit_day=day_idx,
                        entry_date=pos["edate"], exit_date=day_date,
                        entry_price=pos["ep"], exit_price=price,
                        stop_loss=pos["stop_loss"], take_profit=pos["take_profit"],
                        position_value=pos["pv"], risk_dollars=pos["rd"],
                        exit_reason="balance_guard_sell", pnl=pnl,
                        return_pct=pnl / pos["pv"] if pos["pv"] > 0 else 0,
                        regime_at_entry=pos["regime"],
                        ema50_at_entry=pos["ema50"], ema200_at_entry=pos["ema200"],
                        er20_at_entry=pos["er20"], adx14_at_entry=pos["adx14"],
                        rvol_at_entry=pos["rvol"],
                        mfe=mfe, mae=mae,
                        mfe_pct=mfe / pos["ep"] if pos["ep"] > 0 else 0,
                        mae_pct=mae / pos["ep"] if pos["ep"] > 0 else 0,
                        candles_held=day_idx - pos["ed"],
                        price_recovered=rec, close_recovered=crec,
                        stop_distance_pct=0, atr_at_entry=pos["atr"],
                        stop_as_atr=0, regime_after="N/A",
                        reached_1r=reached.get("1r", False),
                        reached_1_5r=reached.get("1.5r", False),
                        reached_2r=reached.get("2r", False),
                        reached_3r=reached.get("3r", False),
                        days_to_1r=dtr.get("1r", -1), days_to_1_5r=dtr.get("1.5r", -1),
                        days_to_2r=dtr.get("2r", -1), days_to_3r=dtr.get("3r", -1),
                    ))
                open_positions = []
                equity.append(balance)
                peak = max(peak, balance)
                if peak > 0: max_dd = max(max_dd, (peak - balance) / peak)
                continue

        # Buy conditions
        if open_positions:
            equity.append(balance)
            peak = max(peak, balance)
            if peak > 0: max_dd = max(max_dd, (peak - balance) / peak)
            continue

        ema200 = float(today.get("ema200", 0) or 0)
        ema50 = float(today.get("ema50", 0) or 0)
        er20 = float(today.get("er20", 0) or 0)
        adx14 = float(today.get("adx14", 0) or 0)
        rvol_val = float(today.get("rvol", 0) or 0)

        buy = False

        if entry_mode == "conservative":
            if balance >= 1110 or balance <= 955 or balance < 975:
                pass
            elif price < ema200:
                pass
            else:
                prev = enriched.iloc[day_idx - 1]
                pc = float(prev.get("close", 0))
                pe = float(prev.get("ema50", 0) or 0)
                if pc and pe and pc > pe:
                    sc = abs(float(today.get("price_change_short", 0) or 0))
                    if sc <= 0.08:
                        if regime == MarketRegime.TREND:
                            stop_dist = 0.03; risk_pct = 0.003
                        elif regime == MarketRegime.CHOP:
                            stop_dist = 0.025; risk_pct = 0.0025
                        elif regime == MarketRegime.LOWVOL:
                            stop_dist = 0.02; risk_pct = 0.0025
                        else:
                            stop_dist = 0; risk_pct = 0

                        if stop_dist > 0:
                            risk_dollars = 1000.0 * risk_pct
                            total_risk = sum(p["rd"] for p in open_positions)
                            if total_risk + risk_dollars <= 1000.0 * 0.01:
                                pv = risk_dollars / stop_dist
                                sl = price * (1 - stop_dist)

                                if balance >= 1090 and regime != MarketRegime.TREND:
                                    pass
                                elif balance >= 1090:
                                    if er20 < 0.5 and adx14 < 25:
                                        pass
                                    else:
                                        pv = min(pv, balance * 0.10)
                                        buy = True
                                else:
                                    if balance < 1050:
                                        pv = min(pv, balance * 0.50)
                                    buy = True

        elif entry_mode == "early_trend":
            if balance >= 1115 or balance <= 955:
                pass
            elif price < ema200:
                pass
            else:
                prev = enriched.iloc[day_idx - 1]
                pc = float(prev.get("close", 0))
                pe = float(prev.get("ema50", 0) or 0)
                if pc and pe and pc > pe:
                    sc = abs(float(today.get("price_change_short", 0) or 0))
                    if sc <= 0.08:
                        is_trend = er20 >= 0.40 and ema50 > ema200
                        is_chop_ok = (regime == MarketRegime.CHOP and
                                      adx14 >= 25 and price > ema50)
                        if is_trend or is_chop_ok:
                            if balance >= 1090:
                                risk_pct = 0.002; stop_dist = 0.02
                            elif is_trend:
                                risk_pct = 0.003; stop_dist = 0.03
                            else:
                                risk_pct = 0.002; stop_dist = 0.025

                            risk_dollars = 1000.0 * risk_pct
                            total_risk = sum(p["rd"] for p in open_positions)
                            if total_risk + risk_dollars <= 1000.0 * 0.015:
                                pv = risk_dollars / stop_dist
                                sl = price * (1 - stop_dist)
                                if balance < 1050:
                                    pv = min(pv, balance * 0.60)
                                buy = True

        if buy:
            pv *= risk_scale
            risk_dollars *= risk_scale
            ep = price * (1 + COMMISSION + SPREAD)
            rpu = price - sl if sl > 0 else price * 0.03
            tp_price = price + rpu * tp_multiple
            atr = compute_atr(enriched, day_idx)

            open_positions.append({
                "symbol": symbol, "ep": ep, "stop_loss": sl,
                "take_profit": tp_price, "pv": pv, "rd": risk_dollars,
                "rpu": rpu, "ed": day_idx, "edate": day_date,
                "regime": regime.value, "ema50": ema50, "ema200": ema200,
                "er20": er20, "adx14": adx14, "rvol": rvol_val, "atr": atr,
            })

        peak = max(peak, balance)
        if peak > 0:
            max_dd = max(max_dd, (peak - balance) / peak)
        equity.append(balance)

    # Close remaining
    for pos in open_positions:
        fp = float(enriched.iloc[-1]["close"])
        cost = pos["pv"] * (COMMISSION + SPREAD)
        pnl = pos["pv"] * (fp - pos["ep"]) / pos["ep"] - cost
        balance += pnl
        tc += 1
        mfe, mae, reached, dtr, rec, crec = excursions(
            enriched, pos["ep"], pos["ed"], len(enriched) - 1, pos["stop_loss"], pos["rpu"])
        trades.append(DetailedTrade(
            trade_id=tc, asset=symbol,
            entry_day=pos["ed"], exit_day=len(enriched) - 1,
            entry_date=pos["edate"],
            exit_date=str(enriched.iloc[-1].get("open_time", ""))[:10],
            entry_price=pos["ep"], exit_price=fp,
            stop_loss=pos["stop_loss"], take_profit=pos["take_profit"],
            position_value=pos["pv"], risk_dollars=pos["rd"],
            exit_reason="end_of_data", pnl=pnl,
            return_pct=pnl / pos["pv"] if pos["pv"] > 0 else 0,
            regime_at_entry=pos["regime"],
            ema50_at_entry=pos["ema50"], ema200_at_entry=pos["ema200"],
            er20_at_entry=pos["er20"], adx14_at_entry=pos["adx14"],
            rvol_at_entry=pos["rvol"],
            mfe=mfe, mae=mae,
            mfe_pct=mfe / pos["ep"] if pos["ep"] > 0 else 0,
            mae_pct=mae / pos["ep"] if pos["ep"] > 0 else 0,
            candles_held=len(enriched) - 1 - pos["ed"],
            price_recovered=False, close_recovered=False,
            stop_distance_pct=(pos["ep"] - pos["stop_loss"]) / pos["ep"] if pos["ep"] > 0 else 0,
            atr_at_entry=pos["atr"],
            stop_as_atr=(pos["ep"] - pos["stop_loss"]) / pos["atr"] if pos["atr"] > 0 else 0,
            regime_after="N/A",
            reached_1r=reached.get("1r", False),
            reached_1_5r=reached.get("1.5r", False),
            reached_2r=reached.get("2r", False),
            reached_3r=reached.get("3r", False),
            days_to_1r=dtr.get("1r", -1), days_to_1_5r=dtr.get("1.5r", -1),
            days_to_2r=dtr.get("2r", -1), days_to_3r=dtr.get("3r", -1),
        ))

    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl <= 0)
    return {
        "trades": trades, "equity": equity,
        "final_balance": balance, "peak_balance": peak,
        "max_drawdown": max_dd, "passed": passed, "failed": failed,
        "total_trades": len(trades), "wins": wins, "losses": losses,
    }


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------

def run_experiment(
    enriched_datasets: dict[str, pd.DataFrame],
    name: str,
    **kwargs,
) -> dict:
    all_trades = []
    balance = STARTING_BALANCE
    per_asset = {}

    for asset, enriched in enriched_datasets.items():
        r = fast_backtest(enriched, asset, starting_balance=balance, **kwargs)
        per_asset[asset] = {
            "trades": r["total_trades"], "wins": r["wins"], "losses": r["losses"],
            "win_rate": r["wins"] / r["total_trades"] if r["total_trades"] > 0 else 0,
            "final_balance": r["final_balance"],
            "max_drawdown": r["max_drawdown"],
            "total_pnl": r["final_balance"] - balance,
            "passed": r["passed"], "failed": r["failed"],
        }
        all_trades.extend(r["trades"])
        balance = r["final_balance"]

    wins = sum(1 for t in all_trades if t.pnl > 0)
    losses = sum(1 for t in all_trades if t.pnl <= 0)
    total = len(all_trades)

    if total > 0:
        avg_w = float(np.mean([t.pnl for t in all_trades if t.pnl > 0])) if wins > 0 else 0
        avg_l = float(np.mean([t.pnl for t in all_trades if t.pnl <= 0])) if losses > 0 else 0
        exp = float(np.mean([t.pnl for t in all_trades]))
        gp = sum(t.pnl for t in all_trades if t.pnl > 0)
        gl = abs(sum(t.pnl for t in all_trades if t.pnl <= 0))
        pf = gp / gl if gl > 0 else float('inf')
    else:
        avg_w = avg_l = exp = pf = 0

    return {
        "experiment": name, "total_trades": total,
        "wins": wins, "losses": losses,
        "win_rate": wins / total if total > 0 else 0,
        "avg_win": avg_w, "avg_loss": avg_l,
        "expectancy": exp, "profit_factor": pf,
        "final_balance": balance,
        "total_pnl": balance - STARTING_BALANCE,
        "per_asset": per_asset, "trades": all_trades,
    }


# ---------------------------------------------------------------------------
# Challenge simulation (block bootstrap)
# ---------------------------------------------------------------------------

def challenge_sim(
    enriched_datasets: dict[str, pd.DataFrame],
    n_sims: int = 50,
    window_days: int = 90,
    **kwargs,
) -> dict:
    rng = np.random.RandomState(42)
    results = []

    for _ in range(n_sims):
        balance = STARTING_BALANCE
        passed = failed = False
        total_trades = 0
        max_dd = 0.0

        asset_list = list(enriched_datasets.keys())
        rng.shuffle(asset_list)

        for asset in asset_list:
            enriched = enriched_datasets[asset]
            n = len(enriched)
            max_start = n - window_days - 200
            if max_start <= 0:
                continue

            start = rng.randint(0, max_start)
            window = enriched.iloc[start:start + 200 + window_days].reset_index(drop=True)

            r = fast_backtest(window, asset, starting_balance=balance, **kwargs)
            balance = r["final_balance"]
            total_trades += r["total_trades"]
            max_dd = max(max_dd, r["max_drawdown"])

            if balance >= WIN_LEVEL:
                passed = True; break
            if balance <= LOSS_LEVEL:
                failed = True; break

        results.append({
            "final_balance": balance, "passed": passed,
            "failed": failed, "trades": total_trades,
            "max_drawdown": max_dd,
        })

    bals = [r["final_balance"] for r in results]
    passes = sum(1 for r in results if r["passed"])
    fails = sum(1 for r in results if r["failed"])

    return {
        "n_simulations": n_sims,
        "pass_rate": passes / n_sims,
        "fail_rate": fails / n_sims,
        "median_balance": float(np.median(bals)),
        "mean_balance": float(np.mean(bals)),
        "p5": float(np.percentile(bals, 5)),
        "p95": float(np.percentile(bals, 95)),
        "avg_max_dd": float(np.mean([r["max_drawdown"] for r in results])),
        "avg_trades": float(np.mean([r["trades"] for r in results])),
    }


# ---------------------------------------------------------------------------
# Loss classification
# ---------------------------------------------------------------------------

def classify_loss(t: DetailedTrade) -> str:
    if t.regime_at_entry in ("CHOP", "LOWVOL"):
        if t.mfe_pct < 0.005:
            return "entry_in_unsuitable_regime"
        elif t.reached_1r:
            return "stop_too_tight"
        else:
            return "genuinely_wrong_direction"
    if t.candles_held <= 2 and t.mfe_pct < 0.003:
        return "late_entry"
    if t.mfe_pct > 0.01 and t.reached_1r:
        return "stop_too_tight"
    if t.candles_held <= 1:
        return "price_spike_noise"
    return "genuinely_wrong_direction"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("CONTROLLED STRATEGY RESEARCH STUDY")
    print("=" * 70)

    # Generate and pre-enrich datasets
    print("\nGenerating and pre-enriching datasets...")
    raw_datasets = {}
    enriched_datasets = {}
    for name, params in ASSETS.items():
        raw = make_market(N_CANDLES, params["base"], params["trend"], params["vol"], params["seed"])
        raw_datasets[name] = raw
        enriched_datasets[name] = compute_indicators(raw)
    print(f"  {len(enriched_datasets)} assets × {N_CANDLES} candles, indicators pre-computed")

    results = {}

    # ===== SECTION 1: Validation =====
    print("\n--- Section 1: Diagnostic Validation ---")
    s1 = {
        "data_type": "SYNTHETIC (geometric Brownian motion)",
        "data_provider": "None — make_market() random walk",
        "candle_timeframe": "1D",
        "commission": f"{COMMISSION*100:.2f}%",
        "spread": f"{SPREAD*100:.1f}%",
        "slippage": "entry = close × (1 + commission + spread)",
        "look_ahead_bias": "NO — indicators computed on data up to current day only",
        "same_candle_execution": "Signal from day N close; stops/TPs checked on day N+1 high/low",
        "mark_to_market": "Cash balance only (conservative — open positions not marked)",
        "multi_asset": "Sequential single-asset runs sharing balance (not simultaneous)",
        "challenge_metric": "Cash balance vs $1,120 win / $950 loss",
        "assets": {},
    }
    for name, df in enriched_datasets.items():
        raw = raw_datasets[name]
        s1["assets"][name] = {
            "candles": len(raw),
            "dates": f"{raw['open_time'].iloc[0].strftime('%Y-%m-%d')} — {raw['open_time'].iloc[-1].strftime('%Y-%m-%d')}",
            "price_range": f"{raw['close'].min():.4f} — {raw['close'].max():.4f}",
            "total_return": f"{(raw['close'].iloc[-1] / raw['close'].iloc[0] - 1) * 100:.2f}%",
            "avg_rvol": f"{df['rvol'].dropna().mean():.4f}",
        }
        print(f"  {name}: {s1['assets'][name]['candles']} candles, {s1['assets'][name]['dates']}, "
              f"return={s1['assets'][name]['total_return']}")

    s1["CRITICAL"] = ("ALL data is synthetic. No fat tails, no correlation, no microstructure. "
                       "Results are directional. Validate on real data before any production change.")
    results["section_1"] = s1

    # ===== SECTION 2: Strategy contradiction =====
    print("\n--- Section 2: Strategy Contradiction ---")
    s2 = {
        "explanation": (
            "The Conservative strategy at engine.py:254-264 INTENTIONALLY allows entries "
            "in CHOP (stop=2.5%, risk=0.25%) and LOWVOL (stop=2%, risk=0.25%). "
            "Only PANIC is blocked (line 223). The EMA200 filter (line 234) requires "
            "price > EMA200 but does NOT require TREND regime. This is by design, not a bug."
        ),
        "backtester_divergence": "NONE — fast_backtest inlines the same logic from engine.py",
        "regime_stability": "Regime classified once per bar; no change between signal and execution",
    }

    # Run baseline and collect trades
    print("\n  Running baseline...")
    baseline = run_experiment(enriched_datasets, "A: Baseline")
    print(f"  Total trades: {baseline['total_trades']}, WR: {baseline['win_rate']:.1%}, "
          f"Exp: ${baseline['expectancy']:.4f}, Final: ${baseline['final_balance']:.2f}")

    # Trade details table
    s2["trade_details"] = []
    for t in baseline["trades"]:
        s2["trade_details"].append({
            "id": t.trade_id, "asset": t.asset,
            "entry_date": t.entry_date, "exit_date": t.exit_date,
            "regime": t.regime_at_entry,
            "entry_price": round(t.entry_price, 6),
            "ema50": round(t.ema50_at_entry, 6),
            "ema200": round(t.ema200_at_entry, 6),
            "er20": round(t.er20_at_entry, 4),
            "adx14": round(t.adx14_at_entry, 2),
            "stop_loss": round(t.stop_loss, 6),
            "take_profit": round(t.take_profit, 6),
            "position_value": round(t.position_value, 2),
            "exit_reason": t.exit_reason,
            "pnl": round(t.pnl, 4),
        })
        print(f"    #{t.trade_id:3d} {t.asset:8s} {t.entry_date} "
              f"regime={t.regime_at_entry:7s} exit={t.exit_reason:15s} P&L=${t.pnl:+.4f}")

    regime_counts = {}
    for t in baseline["trades"]:
        regime_counts[t.regime_at_entry] = regime_counts.get(t.regime_at_entry, 0) + 1
    s2["regime_distribution"] = regime_counts
    print(f"\n  Regime distribution: {regime_counts}")
    results["section_2"] = s2

    # ===== SECTION 3: Stop-loss diagnosis =====
    print("\n--- Section 3: Stop-Loss Diagnosis ---")
    losers = [t for t in baseline["trades"] if t.pnl <= 0]
    categories = {}
    for t in losers:
        cat = classify_loss(t)
        categories.setdefault(cat, []).append(t.trade_id)

    s3 = {
        "total_losers": len(losers),
        "categories": {k: len(v) for k, v in categories.items()},
        "category_ids": {k: v for k, v in categories.items()},
        "avg_mfe_pct": float(np.mean([t.mfe_pct for t in losers])) if losers else 0,
        "avg_mae_pct": float(np.mean([t.mae_pct for t in losers])) if losers else 0,
        "avg_candles_held": float(np.mean([t.candles_held for t in losers])) if losers else 0,
        "pct_recovered": sum(1 for t in losers if t.price_recovered) / len(losers) if losers else 0,
        "pct_close_recovered": sum(1 for t in losers if t.close_recovered) / len(losers) if losers else 0,
        "avg_stop_pct": float(np.mean([t.stop_distance_pct for t in losers])) if losers else 0,
        "avg_stop_atr": float(np.mean([t.stop_as_atr for t in losers if t.stop_as_atr > 0])) if any(t.stop_as_atr > 0 for t in losers) else 0,
        "loser_details": [{
            "id": t.trade_id, "asset": t.asset,
            "mfe_pct": round(t.mfe_pct, 5), "mae_pct": round(t.mae_pct, 5),
            "candles": t.candles_held, "recovered": t.price_recovered,
            "close_recovered": t.close_recovered,
            "atr": round(t.atr_at_entry, 4),
            "stop_pct": round(t.stop_distance_pct, 5),
            "stop_atr": round(t.stop_as_atr, 3),
            "regime": t.regime_at_entry, "regime_after": t.regime_after,
            "category": classify_loss(t),
        } for t in losers],
    }
    print(f"  Losers: {s3['total_losers']}")
    print(f"  Categories: {s3['categories']}")
    print(f"  Avg MFE%: {s3['avg_mfe_pct']:.4%}, Avg MAE%: {s3['avg_mae_pct']:.4%}")
    print(f"  Price recovered after stop: {s3['pct_recovered']:.1%}")
    print(f"  Close recovered after stop: {s3['pct_close_recovered']:.1%}")
    results["section_3"] = s3

    # ===== SECTION 4: Take-profit diagnosis =====
    print("\n--- Section 4: Take-Profit Diagnosis ---")
    all_trades = baseline["trades"]
    r_reach = {
        "1r": sum(1 for t in all_trades if t.reached_1r) / len(all_trades) if all_trades else 0,
        "1.5r": sum(1 for t in all_trades if t.reached_1_5r) / len(all_trades) if all_trades else 0,
        "2r": sum(1 for t in all_trades if t.reached_2r) / len(all_trades) if all_trades else 0,
        "3r": sum(1 for t in all_trades if t.reached_3r) / len(all_trades) if all_trades else 0,
    }

    avg_days = {}
    for label in ["1r", "1_5r", "2r", "3r"]:
        attr = f"days_to_{label}"
        vals = [getattr(t, attr) for t in all_trades if getattr(t, attr) > 0]
        avg_days[label] = float(np.mean(vals)) if vals else -1

    s4 = {
        "r_reach_pct": r_reach,
        "avg_days_to_reach": avg_days,
        "avg_mfe_pct_all": float(np.mean([t.mfe_pct for t in all_trades])),
        "avg_mfe_pct_winners": float(np.mean([t.mfe_pct for t in all_trades if t.pnl > 0])) if any(t.pnl > 0 for t in all_trades) else 0,
        "trade_details": [{
            "id": t.trade_id, "asset": t.asset, "mfe_pct": round(t.mfe_pct, 5),
            "reached_1r": t.reached_1r, "reached_1_5r": t.reached_1_5r,
            "reached_2r": t.reached_2r, "reached_3r": t.reached_3r,
            "days_1r": t.days_to_1r, "days_1_5r": t.days_to_1_5r,
            "days_2r": t.days_to_2r, "days_3r": t.days_to_3r,
        } for t in all_trades],
    }
    print(f"  R-reach rates: {r_reach}")
    print(f"  Avg MFE%: {s4['avg_mfe_pct_all']:.4%}")
    results["section_4"] = s4

    # ===== SECTION 5: Ablation experiments =====
    print("\n--- Section 5: Ablation Experiments ---")
    experiments = {}

    configs = [
        ("A", "A: Baseline", {}),
        ("B", "B: No CHOP", {"regime_filter": {"CHOP"}}),
        ("C", "C: No CHOP/LOWVOL", {"regime_filter": {"CHOP", "LOWVOL"}}),
        ("D", "D: TP=2R", {"tp_multiple": 2.0}),
        ("E", "E: TP=1.5R", {"tp_multiple": 1.5}),
        ("F", "F: Graduated guard", {"balance_guard_mode": "graduated"}),
        ("G", "G: Early Trend", {"entry_mode": "early_trend", "tp_multiple": 2.0}),
    ]

    for key, name, kwargs in configs:
        print(f"  {name}...")
        r = run_experiment(enriched_datasets, name, **kwargs)
        experiments[key] = r
        print(f"    Trades={r['total_trades']:3d} WR={r['win_rate']:5.1%} "
              f"Exp=${r['expectancy']:+.4f} PF={r['profit_factor']:.3f} "
              f"Final=${r['final_balance']:.2f}")

    results["section_5"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "trades"}
        for k, v in experiments.items()
    }

    # ===== SECTION 6: Out-of-sample =====
    print("\n--- Section 6: Out-of-Sample Testing ---")
    oos_results = {}

    for key, name, kwargs in configs:
        print(f"  OOS {name}...")
        oos = {}
        for period, start_pct, end_pct in [("train", 0, 0.6), ("validation", 0.4, 0.8), ("test", 0.6, 1.0)]:
            period_data = {}
            for asset, enriched in enriched_datasets.items():
                n = len(enriched)
                s = max(0, int(n * start_pct) - 200) if start_pct > 0 else 0
                e = int(n * end_pct)
                period_data[asset] = enriched.iloc[s:e].reset_index(drop=True)

            r = run_experiment(period_data, f"{name} ({period})", **kwargs)
            oos[period] = {
                "trades": r["total_trades"], "wins": r["wins"], "losses": r["losses"],
                "win_rate": r["win_rate"], "expectancy": r["expectancy"],
                "profit_factor": r["profit_factor"], "final_balance": r["final_balance"],
                "total_pnl": r["total_pnl"],
            }

        oos_results[key] = oos
        for period in ["train", "validation", "test"]:
            p = oos[period]
            print(f"    {period:12s}: trades={p['trades']:3d} WR={p['win_rate']:5.1%} "
                  f"Exp=${p['expectancy']:+.4f} PF={p['profit_factor']:.3f}")

    results["section_6"] = oos_results

    # ===== SECTION 7: Asset combinations =====
    print("\n--- Section 7: Asset Combinations ---")
    combos = {
        "BTC only": ["BTC/USD"],
        "ETH only": ["ETH/USD"],
        "XRP only": ["XRP/USD"],
        "LINK only": ["LINK/USD"],
        "LTC only": ["LTC/USD"],
        "BTC+ETH": ["BTC/USD", "ETH/USD"],
        "BTC+ETH+LINK": ["BTC/USD", "ETH/USD", "LINK/USD"],
        "All 5": list(enriched_datasets.keys()),
    }

    s7 = {}
    for combo_name, assets in combos.items():
        subset = {k: v for k, v in enriched_datasets.items() if k in assets}
        r = run_experiment(subset, combo_name)
        s7[combo_name] = {
            "assets": assets, "trades": r["total_trades"],
            "wins": r["wins"], "losses": r["losses"],
            "win_rate": r["win_rate"], "expectancy": r["expectancy"],
            "profit_factor": r["profit_factor"], "final_balance": r["final_balance"],
            "total_pnl": r["total_pnl"],
        }
        print(f"  {combo_name:18s}: trades={r['total_trades']:3d} WR={r['win_rate']:5.1%} "
              f"Exp=${r['expectancy']:+.4f} PF={r['profit_factor']:.3f} "
              f"Final=${r['final_balance']:.2f}")

    results["section_7"] = s7

    # ===== SECTION 8: Challenge simulations =====
    print("\n--- Section 8: Challenge Simulations (50 runs) ---")
    s8 = {}
    for key, name, kwargs in configs:
        print(f"  {name}...")
        sim = challenge_sim(enriched_datasets, n_sims=50, **kwargs)
        s8[key] = sim
        print(f"    Pass={sim['pass_rate']:5.1%} Fail={sim['fail_rate']:5.1%} "
              f"Med=${sim['median_balance']:.2f} P5=${sim['p5']:.2f} "
              f"P95=${sim['p95']:.2f} DD={sim['avg_max_dd']:.2%}")

    results["section_8"] = s8

    # ===== SECTION 9: Decision criteria =====
    print("\n--- Section 9: Decision Criteria ---")
    s9 = {}
    for key, name, kwargs in configs:
        exp = results["section_5"].get(key, {})
        sim = s8.get(key, {})
        oos_test = oos_results.get(key, {}).get("test", {})

        s9[key] = {
            "experiment": exp.get("experiment", key),
            "positive_exp_oos": oos_test.get("expectancy", 0) > 0,
            "pf_above_1_oos": oos_test.get("profit_factor", 0) > 1,
            "better_pass_rate": sim.get("pass_rate", 0) > s8.get("A", {}).get("pass_rate", 0),
            "dd_compatible": sim.get("avg_max_dd", 1) < 0.05,
            "sufficient_trades": oos_test.get("trades", 0) >= 5,
            "oos_exp": oos_test.get("expectancy", 0),
            "oos_pf": oos_test.get("profit_factor", 0),
            "pass_rate": sim.get("pass_rate", 0),
            "fail_rate": sim.get("fail_rate", 0),
        }
        s9[key]["passes_all"] = all([
            s9[key]["positive_exp_oos"],
            s9[key]["pf_above_1_oos"],
            s9[key]["better_pass_rate"] or key == "A",
            s9[key]["dd_compatible"],
        ])

        status = "PASS" if s9[key]["passes_all"] else "FAIL"
        print(f"  {key}: [{status}] Exp_OOS=${s9[key]['oos_exp']:+.4f} "
              f"PF_OOS={s9[key]['oos_pf']:.3f} Pass%={s9[key]['pass_rate']:.1%}")

    results["section_9"] = s9

    # ===== SECTION 10: Recommendation =====
    print("\n--- Section 10: Recommendation ---")
    passing = [k for k, v in s9.items() if v["passes_all"] and k != "A"]

    if not passing:
        rec = {
            "action": "KEEP BASELINE — no modification passes all criteria on synthetic data",
            "rationale": (
                "No tested modification showed positive OOS expectancy AND profit factor > 1 "
                "AND better challenge pass rate AND compatible max drawdown. "
                "Sample size may be insufficient, or the strategy needs fundamental redesign. "
                "CRITICAL: All tests ran on synthetic data — validate on real historical data."
            ),
            "passing": [],
            "next_steps": [
                "1. Obtain real historical OHLCV for all 5 assets (min 2 years daily)",
                "2. Re-run this study on real data",
                "3. If still no passing experiment, consider fundamental strategy redesign",
                "4. Do NOT modify production based on synthetic data alone",
            ],
        }
    else:
        best = max(passing, key=lambda k: s9[k]["pass_rate"])
        rec = {
            "action": f"Experiment {best} best candidate, REQUIRES real data validation",
            "rationale": (
                f"{best} passed criteria: OOS exp=${s9[best]['oos_exp']:+.4f}, "
                f"PF={s9[best]['oos_pf']:.3f}, pass%={s9[best]['pass_rate']:.1%}. "
                f"But synthetic data lacks fat tails, correlation, microstructure."
            ),
            "passing": passing,
            "next_steps": [
                "1. Obtain real historical OHLCV data",
                f"2. Re-run experiment {best} on real data",
                "3. If confirmed, implement as paper-only parallel strategy",
                "4. Do NOT replace production until real-data validation complete",
            ],
        }

    results["section_10"] = rec
    print(f"  {rec['action']}")
    for step in rec["next_steps"]:
        print(f"  {step}")

    # Save
    output = Path(__file__).parent / "study_results.json"

    def ser(o):
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, np.ndarray): return o.tolist()
        if isinstance(o, set): return list(o)
        if isinstance(o, DetailedTrade): return {k: ser(v) for k, v in vars(o).items()}
        if isinstance(o, pd.Timestamp): return str(o)
        raise TypeError(f"{type(o)}")

    with open(output, "w") as f:
        json.dump(results, f, indent=2, default=ser)

    print(f"\n  Saved to {output}")
    print("=" * 70)
    return results


if __name__ == "__main__":
    main()
