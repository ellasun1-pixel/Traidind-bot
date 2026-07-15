"""
Enhanced backtester that records per-trade detail needed for the research study.

Records regime at entry, all indicator values, maximum favorable/adverse excursion,
candles to stop, and whether price later recovered — everything tasks 2-4 require.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime

logger = logging.getLogger(__name__)

WIN_LEVEL = 1120.0
LOSS_LEVEL = 950.0
STARTING_BALANCE = 1000.0


@dataclass
class DetailedTrade:
    trade_id: int
    symbol: str
    entry_date: str
    exit_date: str
    entry_day_idx: int
    exit_day_idx: int
    regime_at_entry: str
    entry_rule: str
    entry_price: float
    effective_entry: float
    exit_price: float
    stop_loss: float
    take_profit: float
    position_value: float
    risk_dollars: float
    pnl: float
    exit_reason: str
    ema50_at_entry: float
    ema200_at_entry: float
    er20_at_entry: float
    adx14_at_entry: float
    rvol_at_entry: float
    price_change_short_at_entry: float
    stop_distance_pct: float
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0
    candles_to_exit: int = 0
    price_recovered_after_stop: bool = False
    high_water_mark: float = 0.0
    reached_1r: bool = False
    reached_1_5r: bool = False
    reached_2r: bool = False
    reached_3r: bool = False
    candles_to_1r: int = 0
    candles_to_1_5r: int = 0
    candles_to_2r: int = 0
    candles_to_3r: int = 0
    atr14_at_entry: float = 0.0
    stop_as_atr_multiple: float = 0.0
    regime_after_exit: str = ""
    balance_at_entry: float = 0.0
    balance_at_exit: float = 0.0


@dataclass
class EnhancedBacktestResult:
    strategy_name: str
    symbol: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    max_drawdown_pct: float
    final_balance: float
    challenge_passed: bool
    challenge_failed: bool
    days_simulated: int
    trades: list[DetailedTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    daily_regimes: list[str] = field(default_factory=list)


def _compute_atr(enriched: pd.DataFrame, period: int = 14) -> pd.Series:
    tr1 = enriched["high"] - enriched["low"]
    tr2 = (enriched["high"] - enriched["close"].shift(1)).abs()
    tr3 = (enriched["low"] - enriched["close"].shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def run_enhanced_backtest(
    engine,
    symbol: str,
    daily_df: pd.DataFrame,
    strategy_name: str = "Strategy",
    starting_balance: float = STARTING_BALANCE,
    commission_pct: float = 0.0026,
    spread_pct: float = 0.001,
) -> EnhancedBacktestResult:
    if len(daily_df) < 210:
        return EnhancedBacktestResult(
            strategy_name=strategy_name, symbol=symbol,
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            total_pnl=0.0, max_drawdown_pct=0.0,
            final_balance=starting_balance,
            challenge_passed=False, challenge_failed=False,
            days_simulated=0,
        )

    enriched = compute_indicators(daily_df)
    atr_series = _compute_atr(enriched)
    enriched["atr14"] = atr_series

    balance = starting_balance
    peak = balance
    max_dd = 0.0
    open_positions: list[dict] = []
    detailed_trades: list[DetailedTrade] = []
    equity_curve = [balance]
    daily_regimes = []
    passed = False
    failed = False
    trade_counter = 0
    lookback = 200

    for day_idx in range(lookback, len(enriched)):
        window = enriched.iloc[max(0, day_idx - 299):day_idx + 1]
        today = enriched.iloc[day_idx]
        current_price = float(today["close"])
        high = float(today.get("high", current_price))
        low = float(today.get("low", current_price))

        regime = classify_regime(today)
        daily_regimes.append(regime.value)

        still_open = []
        for pos in open_positions:
            stop = pos["stop_loss"]
            tp = pos.get("take_profit", 0)
            entry = pos["entry_price"]
            risk_per_unit = pos.get("risk_per_unit", entry * 0.03)

            fav = (high - entry) / entry
            adv = (entry - low) / entry
            pos["mfe"] = max(pos.get("mfe", 0), fav)
            pos["mae"] = max(pos.get("mae", 0), adv)
            pos["high_water"] = max(pos.get("high_water", entry), high)

            if risk_per_unit > 0:
                price_gain = high - entry
                if not pos.get("hit_1r") and price_gain >= 1.0 * risk_per_unit:
                    pos["hit_1r"] = True
                    pos["candles_to_1r"] = day_idx - pos["entry_day"]
                if not pos.get("hit_1_5r") and price_gain >= 1.5 * risk_per_unit:
                    pos["hit_1_5r"] = True
                    pos["candles_to_1_5r"] = day_idx - pos["entry_day"]
                if not pos.get("hit_2r") and price_gain >= 2.0 * risk_per_unit:
                    pos["hit_2r"] = True
                    pos["candles_to_2r"] = day_idx - pos["entry_day"]
                if not pos.get("hit_3r") and price_gain >= 3.0 * risk_per_unit:
                    pos["hit_3r"] = True
                    pos["candles_to_3r"] = day_idx - pos["entry_day"]

            if low <= stop:
                exit_price = stop
                cost = pos["position_value"] * (commission_pct + spread_pct)
                pnl = pos["position_value"] * (exit_price - entry) / entry - cost
                balance += pnl

                recovered = False
                for future_idx in range(day_idx + 1, min(day_idx + 30, len(enriched))):
                    if enriched.iloc[future_idx]["high"] >= entry:
                        recovered = True
                        break

                regime_after = ""
                if day_idx + 5 < len(enriched):
                    regime_after = classify_regime(enriched.iloc[day_idx + 5]).value

                trade_counter += 1
                detailed_trades.append(DetailedTrade(
                    trade_id=trade_counter,
                    symbol=symbol,
                    entry_date=str(enriched.iloc[pos["entry_day"]].get("open_time", pos["entry_day"])),
                    exit_date=str(today.get("open_time", day_idx)),
                    entry_day_idx=pos["entry_day"],
                    exit_day_idx=day_idx,
                    regime_at_entry=pos["regime_at_entry"],
                    entry_rule=pos["entry_rule"],
                    entry_price=pos["raw_entry"],
                    effective_entry=entry,
                    exit_price=exit_price,
                    stop_loss=stop,
                    take_profit=tp,
                    position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"],
                    pnl=pnl,
                    exit_reason="stop_loss",
                    ema50_at_entry=pos["ema50"],
                    ema200_at_entry=pos["ema200"],
                    er20_at_entry=pos["er20"],
                    adx14_at_entry=pos["adx14"],
                    rvol_at_entry=pos["rvol"],
                    price_change_short_at_entry=pos["price_change_short"],
                    stop_distance_pct=pos["stop_distance_pct"],
                    max_favorable_excursion=pos.get("mfe", 0),
                    max_adverse_excursion=pos.get("mae", 0),
                    candles_to_exit=day_idx - pos["entry_day"],
                    price_recovered_after_stop=recovered,
                    high_water_mark=pos.get("high_water", entry),
                    reached_1r=pos.get("hit_1r", False),
                    reached_1_5r=pos.get("hit_1_5r", False),
                    reached_2r=pos.get("hit_2r", False),
                    reached_3r=pos.get("hit_3r", False),
                    candles_to_1r=pos.get("candles_to_1r", 0),
                    candles_to_1_5r=pos.get("candles_to_1_5r", 0),
                    candles_to_2r=pos.get("candles_to_2r", 0),
                    candles_to_3r=pos.get("candles_to_3r", 0),
                    atr14_at_entry=pos["atr14"],
                    stop_as_atr_multiple=pos["stop_as_atr_multiple"],
                    regime_after_exit=regime_after,
                    balance_at_entry=pos["balance_at_entry"],
                    balance_at_exit=balance,
                ))
                continue

            if tp and high >= tp:
                exit_price = tp
                cost = pos["position_value"] * (commission_pct + spread_pct)
                pnl = pos["position_value"] * (exit_price - entry) / entry - cost
                balance += pnl

                regime_after = ""
                if day_idx + 5 < len(enriched):
                    regime_after = classify_regime(enriched.iloc[day_idx + 5]).value

                trade_counter += 1
                detailed_trades.append(DetailedTrade(
                    trade_id=trade_counter,
                    symbol=symbol,
                    entry_date=str(enriched.iloc[pos["entry_day"]].get("open_time", pos["entry_day"])),
                    exit_date=str(today.get("open_time", day_idx)),
                    entry_day_idx=pos["entry_day"],
                    exit_day_idx=day_idx,
                    regime_at_entry=pos["regime_at_entry"],
                    entry_rule=pos["entry_rule"],
                    entry_price=pos["raw_entry"],
                    effective_entry=entry,
                    exit_price=exit_price,
                    stop_loss=stop,
                    take_profit=tp,
                    position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"],
                    pnl=pnl,
                    exit_reason="take_profit",
                    ema50_at_entry=pos["ema50"],
                    ema200_at_entry=pos["ema200"],
                    er20_at_entry=pos["er20"],
                    adx14_at_entry=pos["adx14"],
                    rvol_at_entry=pos["rvol"],
                    price_change_short_at_entry=pos["price_change_short"],
                    stop_distance_pct=pos["stop_distance_pct"],
                    max_favorable_excursion=pos.get("mfe", 0),
                    max_adverse_excursion=pos.get("mae", 0),
                    candles_to_exit=day_idx - pos["entry_day"],
                    price_recovered_after_stop=False,
                    high_water_mark=pos.get("high_water", entry),
                    reached_1r=pos.get("hit_1r", False),
                    reached_1_5r=pos.get("hit_1_5r", False),
                    reached_2r=pos.get("hit_2r", False),
                    reached_3r=pos.get("hit_3r", False),
                    candles_to_1r=pos.get("candles_to_1r", 0),
                    candles_to_1_5r=pos.get("candles_to_1_5r", 0),
                    candles_to_2r=pos.get("candles_to_2r", 0),
                    candles_to_3r=pos.get("candles_to_3r", 0),
                    atr14_at_entry=pos["atr14"],
                    stop_as_atr_multiple=pos["stop_as_atr_multiple"],
                    regime_after_exit=regime_after,
                    balance_at_entry=pos["balance_at_entry"],
                    balance_at_exit=balance,
                ))
                continue

            still_open.append(pos)

        open_positions = still_open

        if balance <= LOSS_LEVEL:
            failed = True
            break
        if balance >= WIN_LEVEL:
            passed = True
            break

        total_risk = sum(p["risk_dollars"] for p in open_positions)
        pos_dicts = [
            {"symbol": p["symbol"], "stop_loss": p["stop_loss"],
             "entry_price": p["entry_price"], "risk_per_unit": p.get("risk_per_unit", 0),
             "status": "open"}
            for p in open_positions
        ]

        signal = engine.analyze(
            symbol, window, window, current_price,
            balance, pos_dicts, total_risk,
        )

        if signal.signal_type == "BUY" and signal.position_size_usd > 0:
            slippage_pct = commission_pct + spread_pct
            effective_entry = current_price * (1 + slippage_pct)
            stop_distance = current_price - signal.stop_loss
            risk_per_unit = stop_distance if stop_distance > 0 else current_price * 0.03
            stop_distance_pct = stop_distance / current_price if current_price > 0 else 0.03

            tp_price = 0.0
            if hasattr(engine, 'cfg'):
                tp_price = current_price + risk_per_unit * engine.cfg.take_profit_multiple
            elif hasattr(engine, 'take_profit_multiple'):
                tp_price = current_price + risk_per_unit * engine.take_profit_multiple

            atr_val = float(today.get("atr14", 0) or 0)
            stop_as_atr = (stop_distance / atr_val) if atr_val > 0 else 0.0

            entry_rule = f"Regime={regime.value}, price>EMA200, prev_close>EMA50"
            if regime == MarketRegime.TREND:
                entry_rule += ", TREND entry (0.3% risk, 3% stop)"
            elif regime == MarketRegime.CHOP:
                entry_rule += ", CHOP entry (0.25% risk, 2.5% stop)"
            elif regime == MarketRegime.LOWVOL:
                entry_rule += ", LOWVOL entry (0.25% risk, 2% stop)"

            open_positions.append({
                "symbol": symbol,
                "entry_price": effective_entry,
                "raw_entry": current_price,
                "stop_loss": signal.stop_loss,
                "take_profit": tp_price,
                "position_value": signal.position_size_usd,
                "risk_dollars": signal.max_loss_usd,
                "risk_per_unit": risk_per_unit,
                "entry_day": day_idx,
                "regime_at_entry": regime.value,
                "entry_rule": entry_rule,
                "ema50": float(today.get("ema50", 0) or 0),
                "ema200": float(today.get("ema200", 0) or 0),
                "er20": float(today.get("er20", 0) or 0),
                "adx14": float(today.get("adx14", 0) or 0),
                "rvol": float(today.get("rvol", 0) or 0),
                "price_change_short": float(today.get("price_change_short", 0) or 0),
                "stop_distance_pct": stop_distance_pct,
                "atr14": atr_val,
                "stop_as_atr_multiple": stop_as_atr,
                "balance_at_entry": balance,
                "mfe": 0.0,
                "mae": 0.0,
                "high_water": effective_entry,
            })

        elif signal.signal_type == "SELL" and open_positions:
            for pos in open_positions:
                cost = pos["position_value"] * (commission_pct + spread_pct)
                entry = pos["entry_price"]
                pnl = pos["position_value"] * (current_price - entry) / entry - cost
                balance += pnl

                regime_after = ""
                if day_idx + 5 < len(enriched):
                    regime_after = classify_regime(enriched.iloc[day_idx + 5]).value

                trade_counter += 1
                detailed_trades.append(DetailedTrade(
                    trade_id=trade_counter,
                    symbol=symbol,
                    entry_date=str(enriched.iloc[pos["entry_day"]].get("open_time", pos["entry_day"])),
                    exit_date=str(today.get("open_time", day_idx)),
                    entry_day_idx=pos["entry_day"],
                    exit_day_idx=day_idx,
                    regime_at_entry=pos["regime_at_entry"],
                    entry_rule=pos["entry_rule"],
                    entry_price=pos["raw_entry"],
                    effective_entry=entry,
                    exit_price=current_price,
                    stop_loss=pos["stop_loss"],
                    take_profit=pos.get("take_profit", 0),
                    position_value=pos["position_value"],
                    risk_dollars=pos["risk_dollars"],
                    pnl=pnl,
                    exit_reason="signal_sell",
                    ema50_at_entry=pos["ema50"],
                    ema200_at_entry=pos["ema200"],
                    er20_at_entry=pos["er20"],
                    adx14_at_entry=pos["adx14"],
                    rvol_at_entry=pos["rvol"],
                    price_change_short_at_entry=pos["price_change_short"],
                    stop_distance_pct=pos["stop_distance_pct"],
                    max_favorable_excursion=pos.get("mfe", 0),
                    max_adverse_excursion=pos.get("mae", 0),
                    candles_to_exit=day_idx - pos["entry_day"],
                    price_recovered_after_stop=False,
                    high_water_mark=pos.get("high_water", entry),
                    reached_1r=pos.get("hit_1r", False),
                    reached_1_5r=pos.get("hit_1_5r", False),
                    reached_2r=pos.get("hit_2r", False),
                    reached_3r=pos.get("hit_3r", False),
                    candles_to_1r=pos.get("candles_to_1r", 0),
                    candles_to_1_5r=pos.get("candles_to_1_5r", 0),
                    candles_to_2r=pos.get("candles_to_2r", 0),
                    candles_to_3r=pos.get("candles_to_3r", 0),
                    atr14_at_entry=pos["atr14"],
                    stop_as_atr_multiple=pos["stop_as_atr_multiple"],
                    regime_after_exit=regime_after,
                    balance_at_entry=pos["balance_at_entry"],
                    balance_at_exit=balance,
                ))
            open_positions = []

        peak = max(peak, balance)
        if peak > 0:
            dd = (peak - balance) / peak
            max_dd = max(max_dd, dd)
        equity_curve.append(balance)

    for pos in open_positions:
        final_price = float(enriched.iloc[-1]["close"])
        cost = pos["position_value"] * (commission_pct + spread_pct)
        entry = pos["entry_price"]
        pnl = pos["position_value"] * (final_price - entry) / entry - cost
        balance += pnl

        trade_counter += 1
        detailed_trades.append(DetailedTrade(
            trade_id=trade_counter,
            symbol=symbol,
            entry_date=str(enriched.iloc[pos["entry_day"]].get("open_time", pos["entry_day"])),
            exit_date=str(enriched.iloc[-1].get("open_time", len(enriched) - 1)),
            entry_day_idx=pos["entry_day"],
            exit_day_idx=len(enriched) - 1,
            regime_at_entry=pos["regime_at_entry"],
            entry_rule=pos["entry_rule"],
            entry_price=pos["raw_entry"],
            effective_entry=entry,
            exit_price=final_price,
            stop_loss=pos["stop_loss"],
            take_profit=pos.get("take_profit", 0),
            position_value=pos["position_value"],
            risk_dollars=pos["risk_dollars"],
            pnl=pnl,
            exit_reason="end_of_data",
            ema50_at_entry=pos["ema50"],
            ema200_at_entry=pos["ema200"],
            er20_at_entry=pos["er20"],
            adx14_at_entry=pos["adx14"],
            rvol_at_entry=pos["rvol"],
            price_change_short_at_entry=pos["price_change_short"],
            stop_distance_pct=pos["stop_distance_pct"],
            max_favorable_excursion=pos.get("mfe", 0),
            max_adverse_excursion=pos.get("mae", 0),
            candles_to_exit=len(enriched) - 1 - pos["entry_day"],
            price_recovered_after_stop=False,
            high_water_mark=pos.get("high_water", entry),
            reached_1r=pos.get("hit_1r", False),
            reached_1_5r=pos.get("hit_1_5r", False),
            reached_2r=pos.get("hit_2r", False),
            reached_3r=pos.get("hit_3r", False),
            candles_to_1r=pos.get("candles_to_1r", 0),
            candles_to_1_5r=pos.get("candles_to_1_5r", 0),
            candles_to_2r=pos.get("candles_to_2r", 0),
            candles_to_3r=pos.get("candles_to_3r", 0),
            atr14_at_entry=pos["atr14"],
            stop_as_atr_multiple=pos["stop_as_atr_multiple"],
            regime_after_exit="",
            balance_at_entry=pos["balance_at_entry"],
            balance_at_exit=balance,
        ))

    wins = sum(1 for t in detailed_trades if t.pnl > 0)
    losses = sum(1 for t in detailed_trades if t.pnl <= 0)
    total = len(detailed_trades)

    return EnhancedBacktestResult(
        strategy_name=strategy_name,
        symbol=symbol,
        total_trades=total,
        wins=wins,
        losses=losses,
        win_rate=wins / total if total > 0 else 0.0,
        total_pnl=balance - starting_balance,
        max_drawdown_pct=max_dd,
        final_balance=balance,
        challenge_passed=passed,
        challenge_failed=failed,
        days_simulated=len(equity_curve),
        trades=detailed_trades,
        equity_curve=equity_curve,
        daily_regimes=daily_regimes,
    )
