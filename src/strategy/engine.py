from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.config import settings
from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    signal_type: str  # BUY/SELL/REDUCE/TAKE_PROFIT/MOVE_TO_USD/NO_TRADE
    priority: str  # CRITICAL/HIGH/MEDIUM
    asset_symbol: str
    regime: MarketRegime
    entry_price: float = 0.0
    stop_loss: float = 0.0
    position_size_usd: float = 0.0
    max_loss_usd: float = 0.0
    order_type: str = "LIMIT"
    cancel_level: float = 0.0
    reason: str = ""
    explanation: str = ""
    price_range_low: float = 0.0
    price_range_high: float = 0.0
    remaining_usd: float = 0.0
    current_balance: float = 0.0
    distance_to_win: float = 0.0
    distance_to_loss: float = 0.0
    provider: str = ""


class StrategyEngine:
    def __init__(self):
        self.take_profit_multiple = settings.take_profit_risk_multiple

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
            from src.strategy.regime import regime_nan_fields
            nan_fields = regime_nan_fields(latest)
            return self._no_trade(
                symbol, regime, portfolio_balance,
                f"Data insufficient — NaN in: {', '.join(nan_fields)}",
            )

        existing = [p for p in open_positions if p.get("symbol") == symbol]

        sell_signal = self._check_sell_conditions(
            symbol, regime, latest, current_price, existing, portfolio_balance
        )
        if sell_signal:
            return sell_signal

        tp_signal = self._check_take_profit(
            symbol, regime, current_price, existing, portfolio_balance
        )
        if tp_signal:
            return tp_signal

        buy_signal = self._check_buy_conditions(
            symbol, regime, latest, prev, current_price, daily,
            portfolio_balance, existing, open_positions, total_open_risk_usd,
        )
        if buy_signal:
            return buy_signal

        return self._no_trade(symbol, regime, portfolio_balance, "No actionable signal")

    def _check_sell_conditions(
        self,
        symbol: str,
        regime: MarketRegime,
        latest: pd.Series,
        current_price: float,
        existing: list[dict],
        balance: float,
    ) -> Optional[TradeSignal]:
        if not existing:
            return None

        for pos in existing:
            stop_loss = pos.get("stop_loss", 0)
            entry = pos.get("entry_price", 0)
            risk_per_unit = pos.get("risk_per_unit", 0)
            if entry > 0 and risk_per_unit > 0:
                breakeven_threshold = entry + risk_per_unit * 1.5
                if current_price >= breakeven_threshold:
                    stop_loss = max(stop_loss, entry)
            if stop_loss and current_price <= stop_loss:
                return TradeSignal(
                    signal_type="SELL",
                    priority="CRITICAL",
                    asset_symbol=symbol,
                    regime=regime,
                    entry_price=current_price,
                    reason="Stop-loss level breached",
                    explanation="Price hit your protective stop — sell to limit losses",
                    current_balance=balance,
                    distance_to_win=settings.win_level - balance,
                    distance_to_loss=balance - settings.loss_level,
                )

        if regime == MarketRegime.PANIC:
            return TradeSignal(
                signal_type="SELL",
                priority="CRITICAL",
                asset_symbol=symbol,
                regime=regime,
                entry_price=current_price,
                reason="Market entered PANIC regime",
                explanation="Major crash detected — exit positions to protect capital",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        if balance < 965:
            return TradeSignal(
                signal_type="MOVE_TO_USD",
                priority="CRITICAL",
                asset_symbol=symbol,
                regime=regime,
                entry_price=current_price,
                reason="Balance below $965 — move to USD recommended",
                explanation="Strongly recommended to go fully to cash to avoid defeat",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        if balance < 975:
            return TradeSignal(
                signal_type="SELL",
                priority="CRITICAL",
                asset_symbol=symbol,
                regime=regime,
                entry_price=current_price,
                reason="Balance dangerously close to loss level — exit all risk",
                explanation="Your balance is near $950 defeat — sell to protect remaining capital",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        return None

    def _check_take_profit(
        self,
        symbol: str,
        regime: MarketRegime,
        current_price: float,
        existing: list[dict],
        balance: float,
    ) -> Optional[TradeSignal]:
        if not existing:
            return None

        for pos in existing:
            entry = pos.get("entry_price", 0)
            risk_per_unit = pos.get("risk_per_unit", 0)
            if entry <= 0:
                continue
            profit_pct = (current_price - entry) / entry
            if risk_per_unit > 0:
                profit_units = (current_price - entry) / risk_per_unit
                if profit_units >= self.take_profit_multiple:
                    return TradeSignal(
                        signal_type="TAKE_PROFIT",
                        priority="HIGH",
                        asset_symbol=symbol,
                        regime=regime,
                        entry_price=current_price,
                        reason=f"Profit reached {profit_units:.1f}x risk",
                        explanation=f"You've earned {profit_units:.1f} times what you risked — lock it in",
                        current_balance=balance,
                        distance_to_win=settings.win_level - balance,
                        distance_to_loss=balance - settings.loss_level,
                    )
            elif profit_pct > 0.05:
                return TradeSignal(
                    signal_type="TAKE_PROFIT",
                    priority="HIGH",
                    asset_symbol=symbol,
                    regime=regime,
                    entry_price=current_price,
                    reason=f"Significant profit: {profit_pct*100:.1f}%",
                    explanation="Solid profit accumulated — consider taking it",
                    current_balance=balance,
                    distance_to_win=settings.win_level - balance,
                    distance_to_loss=balance - settings.loss_level,
                )
        return None

    def _check_buy_conditions(
        self,
        symbol: str,
        regime: MarketRegime,
        latest: pd.Series,
        prev: pd.Series,
        current_price: float,
        daily: pd.DataFrame,
        balance: float,
        existing: list[dict],
        all_positions: list[dict],
        total_open_risk_usd: float,
    ) -> Optional[TradeSignal]:
        if regime == MarketRegime.PANIC:
            return None

        if balance >= 1110:
            return None
        if balance <= 955:
            return None
        if balance < 975:
            return None

        if not self._is_closed_candle_confirmation(latest, prev):
            return None

        short_change = abs(latest.get("price_change_short", 0) or 0)
        if short_change > settings.vertical_spike_pct:
            return None

        open_count = len([p for p in all_positions if p.get("status") == "open"])
        if open_count >= settings.max_open_positions:
            return None

        if existing:
            return None

        commission_spread = settings.commission_pct + settings.spread_pct
        min_expected_profit_pct = commission_spread * 3

        if regime == MarketRegime.TREND:
            stop_distance_pct = 0.03
            risk_pct = settings.risk_per_trade_pct_default
        elif regime == MarketRegime.CHOP:
            stop_distance_pct = 0.025
            risk_pct = settings.risk_per_trade_pct_min
        elif regime == MarketRegime.LOWVOL:
            stop_distance_pct = 0.02
            risk_pct = settings.risk_per_trade_pct_min
        else:
            return None

        risk_dollars = settings.starting_balance * risk_pct
        max_total_risk = settings.starting_balance * settings.max_total_open_risk_pct
        if total_open_risk_usd + risk_dollars > max_total_risk:
            return None

        position_value = risk_dollars / stop_distance_pct
        stop_loss_price = current_price * (1 - stop_distance_pct)

        if balance >= 1090:
            if regime != MarketRegime.TREND:
                return None
            er20 = latest.get("er20", 0) or 0
            adx_val = latest.get("adx14", 0) or 0
            if er20 < 0.5 and adx_val < 25:
                return None
            position_value = min(position_value, balance * 0.10)

        if balance < 1050:
            max_invested = balance * 0.50
            position_value = min(position_value, max_invested)

        price_range_low = current_price * 0.998
        price_range_high = current_price * 1.002

        return TradeSignal(
            signal_type="BUY",
            priority="MEDIUM" if regime != MarketRegime.TREND else "HIGH",
            asset_symbol=symbol,
            regime=regime,
            entry_price=current_price,
            stop_loss=stop_loss_price,
            position_size_usd=round(position_value, 2),
            max_loss_usd=round(risk_dollars, 2),
            order_type="LIMIT",
            cancel_level=round(current_price * 1.01, 2),
            reason=f"Regime={regime.value}, EMA200 trend confirmed, risk within budget",
            explanation="Trend looks favorable and risk is managed — consider a small position",
            price_range_low=round(price_range_low, 2),
            price_range_high=round(price_range_high, 2),
            remaining_usd=round(balance - position_value, 2),
            current_balance=balance,
            distance_to_win=round(settings.win_level - balance, 2),
            distance_to_loss=round(balance - settings.loss_level, 2),
        )

    def _is_closed_candle_confirmation(self, latest: pd.Series, prev: pd.Series) -> bool:
        prev_close = prev.get("close", 0)
        prev_ema50 = prev.get("ema50", 0)
        if prev_close and prev_ema50 and prev_close > prev_ema50:
            return True
        return False

    def _no_trade(
        self, symbol: str, regime: MarketRegime, balance: float, reason: str
    ) -> TradeSignal:
        return TradeSignal(
            signal_type="NO_TRADE",
            priority="MEDIUM",
            asset_symbol=symbol,
            regime=regime,
            reason=reason,
            current_balance=balance,
            distance_to_win=round(settings.win_level - balance, 2),
            distance_to_loss=round(balance - settings.loss_level, 2),
        )
