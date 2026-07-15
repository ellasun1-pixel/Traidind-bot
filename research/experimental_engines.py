"""
Experimental strategy engine variants for ablation testing.

Each variant changes exactly ONE thing from the baseline Conservative engine.
These are research-only — none modify production code.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from src.strategy.indicators import compute_indicators
from src.strategy.regime import classify_regime, MarketRegime
from src.strategy.engine import TradeSignal, StrategyEngine
from src.config import settings


class ExperimentB_NoCHOP(StrategyEngine):
    """Baseline but blocks CHOP entries."""

    def _check_buy_conditions(self, symbol, regime, latest, prev, current_price,
                               daily, balance, existing, all_positions, total_open_risk_usd):
        if regime == MarketRegime.CHOP:
            return None
        return super()._check_buy_conditions(
            symbol, regime, latest, prev, current_price,
            daily, balance, existing, all_positions, total_open_risk_usd,
        )


class ExperimentC_TrendOnly(StrategyEngine):
    """Baseline but blocks CHOP and LOWVOL entries."""

    def _check_buy_conditions(self, symbol, regime, latest, prev, current_price,
                               daily, balance, existing, all_positions, total_open_risk_usd):
        if regime in (MarketRegime.CHOP, MarketRegime.LOWVOL):
            return None
        return super()._check_buy_conditions(
            symbol, regime, latest, prev, current_price,
            daily, balance, existing, all_positions, total_open_risk_usd,
        )


class ExperimentD_TP2R(StrategyEngine):
    """Baseline but take-profit at 2R instead of 3R."""

    def __init__(self):
        super().__init__()
        self.take_profit_multiple = 2.0


class ExperimentE_TP1_5R(StrategyEngine):
    """Baseline but take-profit at 1.5R instead of 3R."""

    def __init__(self):
        super().__init__()
        self.take_profit_multiple = 1.5


class ExperimentF_GraduatedRisk(StrategyEngine):
    """Baseline but replaces $975 hard stop with graduated risk reduction."""

    def _check_buy_conditions(self, symbol, regime, latest, prev, current_price,
                               daily, balance, existing, all_positions, total_open_risk_usd):
        if regime == MarketRegime.PANIC:
            return None
        if balance >= 1110:
            return None
        if balance <= 955:
            return None

        # Graduated risk instead of hard $975 cutoff
        if balance < 965:
            return None

        ema200_val = latest.get("ema200", 0) or 0
        if current_price < ema200_val:
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

        # Graduated risk reduction
        if balance < 975:
            risk_pct *= 0.25
        elif balance < 985:
            risk_pct *= 0.50

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
            reason=f"ExpF: Regime={regime.value}, graduated risk",
            explanation="Graduated risk experiment",
            price_range_low=round(current_price * 0.998, 2),
            price_range_high=round(current_price * 1.002, 2),
            remaining_usd=round(balance - position_value, 2),
            current_balance=balance,
            distance_to_win=round(settings.win_level - balance, 2),
            distance_to_loss=round(balance - settings.loss_level, 2),
        )

    def _check_sell_conditions(self, symbol, regime, latest, current_price,
                                existing, balance):
        if not existing:
            return None

        for pos in existing:
            stop_loss = pos.get("stop_loss", 0)
            if stop_loss and current_price <= stop_loss:
                return TradeSignal(
                    signal_type="SELL", priority="CRITICAL",
                    asset_symbol=symbol, regime=regime, entry_price=current_price,
                    reason="Stop-loss breached",
                    current_balance=balance,
                    distance_to_win=settings.win_level - balance,
                    distance_to_loss=balance - settings.loss_level,
                )

        if regime == MarketRegime.PANIC:
            return TradeSignal(
                signal_type="SELL", priority="CRITICAL",
                asset_symbol=symbol, regime=regime, entry_price=current_price,
                reason="PANIC regime",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        # Graduated: only force-sell below $965 instead of $975
        if balance < 965:
            return TradeSignal(
                signal_type="SELL", priority="CRITICAL",
                asset_symbol=symbol, regime=regime, entry_price=current_price,
                reason="Balance below $965 — graduated exit",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        return None


class ExperimentG_EarlyTrend(StrategyEngine):
    """Adds early trend entries with relaxed ER/ADX requirements and reduced size."""

    def _check_buy_conditions(self, symbol, regime, latest, prev, current_price,
                               daily, balance, existing, all_positions, total_open_risk_usd):
        # Try standard entry first
        standard = super()._check_buy_conditions(
            symbol, regime, latest, prev, current_price,
            daily, balance, existing, all_positions, total_open_risk_usd,
        )
        if standard:
            return standard

        # Early trend conditions
        if regime == MarketRegime.PANIC:
            return None
        if balance >= 1110 or balance <= 955 or balance < 975:
            return None
        if existing:
            return None

        er20 = latest.get("er20", 0) or 0
        adx_val = latest.get("adx14", 0) or 0
        ema50_val = latest.get("ema50", 0) or 0

        if er20 < 0.40:
            return None
        if adx_val < 25:
            return None
        if current_price <= ema50_val:
            return None

        if not self._is_closed_candle_confirmation(latest, prev):
            return None

        short_change = abs(latest.get("price_change_short", 0) or 0)
        if short_change > settings.vertical_spike_pct:
            return None

        open_count = len([p for p in all_positions if p.get("status") == "open"])
        if open_count >= settings.max_open_positions:
            return None

        # Reduced risk for early trend
        risk_pct = settings.risk_per_trade_pct_min * 0.75
        stop_distance_pct = 0.025
        risk_dollars = settings.starting_balance * risk_pct
        max_total_risk = settings.starting_balance * settings.max_total_open_risk_pct
        if total_open_risk_usd + risk_dollars > max_total_risk:
            return None

        position_value = risk_dollars / stop_distance_pct
        stop_loss_price = current_price * (1 - stop_distance_pct)

        if balance < 1050:
            position_value = min(position_value, balance * 0.50)

        return TradeSignal(
            signal_type="BUY",
            priority="MEDIUM",
            asset_symbol=symbol,
            regime=regime,
            entry_price=current_price,
            stop_loss=stop_loss_price,
            position_size_usd=round(position_value, 2),
            max_loss_usd=round(risk_dollars, 2),
            order_type="LIMIT",
            cancel_level=round(current_price * 1.01, 2),
            reason=f"ExpG: EarlyTrend ER20={er20:.2f} ADX={adx_val:.0f}",
            explanation="Early trend experiment",
            price_range_low=round(current_price * 0.998, 2),
            price_range_high=round(current_price * 1.002, 2),
            remaining_usd=round(balance - position_value, 2),
            current_balance=balance,
            distance_to_win=round(settings.win_level - balance, 2),
            distance_to_loss=round(balance - settings.loss_level, 2),
        )
