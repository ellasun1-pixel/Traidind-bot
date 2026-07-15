"""
Challenge strategy — more aggressive than Conservative, tuned for the
$1000 → $1120 Kraken Funded Challenge with $950 loss boundary.

Key differences from Conservative:
- Accepts TREND trades at ER20 ≥ 0.30 (vs 0.35)
- Trades in CHOP if ADX > 20 and price > EMA50
- Wider balance range for entry (955–1115 vs 955–1110)
- Larger position sizes (0.5% risk default vs 0.3%)
- Take profit at 2× risk (vs 3×) — lock profits faster
- Allows up to 3 open positions (vs 2)
- Dynamic stop tightening near the win level

Does NOT modify the Conservative strategy in any way.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from src.strategy.engine import TradeSignal

logger = logging.getLogger(__name__)

WIN_LEVEL = 1120.0
LOSS_LEVEL = 950.0
STARTING_BALANCE = 1000.0


@dataclass
class ChallengeConfig:
    er_trend_threshold: float = 0.30
    chop_adx_min: float = 20.0
    risk_per_trade_pct: float = 0.005
    risk_per_trade_pct_cautious: float = 0.003
    max_total_open_risk_pct: float = 0.015
    max_open_positions: int = 3
    take_profit_multiple: float = 2.0
    vertical_spike_pct: float = 0.08
    commission_pct: float = 0.0026
    spread_pct: float = 0.001


class ChallengeStrategyEngine:
    def __init__(self, config: ChallengeConfig | None = None):
        self.cfg = config or ChallengeConfig()

    def analyze(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        h4_df: pd.DataFrame,
        current_price: float,
        portfolio_balance: float,
        open_positions: list[dict],
        total_open_risk_usd: float,
    ) -> TradeSignal:
        if daily_df.empty or len(daily_df) < 200:
            return self._no_trade(symbol, MarketRegime.CHOP, portfolio_balance, "Insufficient data")

        daily = compute_indicators(daily_df)
        latest = daily.iloc[-1]
        prev = daily.iloc[-2] if len(daily) > 1 else latest
        regime = classify_regime(latest)

        if regime == MarketRegime.DATA_INSUFFICIENT:
            return self._no_trade(symbol, regime, portfolio_balance, "Data insufficient for classification")

        existing = [p for p in open_positions if p.get("symbol") == symbol]

        sell_signal = self._check_sell(symbol, regime, latest, current_price, existing, portfolio_balance)
        if sell_signal:
            return sell_signal

        tp_signal = self._check_take_profit(symbol, regime, current_price, existing, portfolio_balance)
        if tp_signal:
            return tp_signal

        buy_signal = self._check_buy(
            symbol, regime, latest, prev, current_price, daily,
            portfolio_balance, existing, open_positions, total_open_risk_usd,
        )
        if buy_signal:
            return buy_signal

        return self._no_trade(symbol, regime, portfolio_balance, "No actionable signal")

    def _check_sell(self, symbol, regime, latest, price, existing, balance) -> Optional[TradeSignal]:
        if not existing:
            return None

        for pos in existing:
            stop = pos.get("stop_loss", 0)
            if stop and price <= stop:
                return TradeSignal(
                    signal_type="SELL", priority="CRITICAL",
                    asset_symbol=symbol, regime=regime, entry_price=price,
                    reason="Stop-loss breached",
                    current_balance=balance,
                    distance_to_win=WIN_LEVEL - balance,
                    distance_to_loss=balance - LOSS_LEVEL,
                )

        if regime == MarketRegime.PANIC:
            return TradeSignal(
                signal_type="SELL", priority="CRITICAL",
                asset_symbol=symbol, regime=regime, entry_price=price,
                reason="PANIC regime — exit",
                current_balance=balance,
                distance_to_win=WIN_LEVEL - balance,
                distance_to_loss=balance - LOSS_LEVEL,
            )

        if balance < 965:
            return TradeSignal(
                signal_type="SELL", priority="CRITICAL",
                asset_symbol=symbol, regime=regime, entry_price=price,
                reason="Balance near loss level",
                current_balance=balance,
                distance_to_win=WIN_LEVEL - balance,
                distance_to_loss=balance - LOSS_LEVEL,
            )

        return None

    def _check_take_profit(self, symbol, regime, price, existing, balance) -> Optional[TradeSignal]:
        if not existing:
            return None

        for pos in existing:
            entry = pos.get("entry_price", 0)
            risk_per_unit = pos.get("risk_per_unit", 0)
            if entry <= 0:
                continue

            if risk_per_unit > 0:
                profit_units = (price - entry) / risk_per_unit
                if profit_units >= self.cfg.take_profit_multiple:
                    return TradeSignal(
                        signal_type="TAKE_PROFIT", priority="HIGH",
                        asset_symbol=symbol, regime=regime, entry_price=price,
                        reason=f"Profit {profit_units:.1f}× risk",
                        current_balance=balance,
                        distance_to_win=WIN_LEVEL - balance,
                        distance_to_loss=balance - LOSS_LEVEL,
                    )
            else:
                profit_pct = (price - entry) / entry
                if profit_pct > 0.03:
                    return TradeSignal(
                        signal_type="TAKE_PROFIT", priority="HIGH",
                        asset_symbol=symbol, regime=regime, entry_price=price,
                        reason=f"Profit {profit_pct*100:.1f}%",
                        current_balance=balance,
                        distance_to_win=WIN_LEVEL - balance,
                        distance_to_loss=balance - LOSS_LEVEL,
                    )
        return None

    def _check_buy(
        self, symbol, regime, latest, prev, price, daily,
        balance, existing, all_positions, total_risk,
    ) -> Optional[TradeSignal]:
        if regime == MarketRegime.PANIC:
            return None
        if balance >= 1115:
            return None
        if balance <= 955:
            return None

        ema200 = float(latest.get("ema200", 0) or 0)
        ema50 = float(latest.get("ema50", 0) or 0)
        er20 = float(latest.get("er20", 0) or 0)
        adx14 = float(latest.get("adx14", 0) or 0)

        if price < ema200:
            return None

        prev_close = float(prev.get("close", 0))
        prev_ema50 = float(prev.get("ema50", 0) or 0)
        if not (prev_close and prev_ema50 and prev_close > prev_ema50):
            return None

        short_change = abs(float(latest.get("price_change_short", 0) or 0))
        if short_change > self.cfg.vertical_spike_pct:
            return None

        open_count = len([p for p in all_positions if p.get("status") == "open"])
        if open_count >= self.cfg.max_open_positions:
            return None

        if existing:
            return None

        is_trend = er20 >= self.cfg.er_trend_threshold and ema50 > ema200
        is_chop_tradeable = (
            regime == MarketRegime.CHOP
            and adx14 >= self.cfg.chop_adx_min
            and price > ema50
        )

        if not (is_trend or is_chop_tradeable):
            return None

        if balance >= 1090:
            risk_pct = self.cfg.risk_per_trade_pct_cautious
            stop_distance_pct = 0.02
        elif is_trend:
            risk_pct = self.cfg.risk_per_trade_pct
            stop_distance_pct = 0.03
        else:
            risk_pct = self.cfg.risk_per_trade_pct_cautious
            stop_distance_pct = 0.025

        risk_dollars = STARTING_BALANCE * risk_pct
        max_total_risk = STARTING_BALANCE * self.cfg.max_total_open_risk_pct
        if total_risk + risk_dollars > max_total_risk:
            return None

        position_value = risk_dollars / stop_distance_pct
        stop_loss_price = price * (1 - stop_distance_pct)

        if balance < 1050:
            position_value = min(position_value, balance * 0.60)

        return TradeSignal(
            signal_type="BUY",
            priority="HIGH" if is_trend else "MEDIUM",
            asset_symbol=symbol,
            regime=regime,
            entry_price=price,
            stop_loss=stop_loss_price,
            position_size_usd=round(position_value, 2),
            max_loss_usd=round(risk_dollars, 2),
            order_type="LIMIT",
            cancel_level=round(price * 1.01, 2),
            reason=f"Challenge: regime={regime.value}, ER20={er20:.2f}, ADX={adx14:.0f}",
            explanation="Challenge strategy entry",
            price_range_low=round(price * 0.998, 2),
            price_range_high=round(price * 1.002, 2),
            remaining_usd=round(balance - position_value, 2),
            current_balance=balance,
            distance_to_win=round(WIN_LEVEL - balance, 2),
            distance_to_loss=round(balance - LOSS_LEVEL, 2),
        )

    def _no_trade(self, symbol, regime, balance, reason):
        return TradeSignal(
            signal_type="NO_TRADE", priority="MEDIUM",
            asset_symbol=symbol, regime=regime, reason=reason,
            current_balance=balance,
            distance_to_win=round(WIN_LEVEL - balance, 2),
            distance_to_loss=round(balance - LOSS_LEVEL, 2),
        )
