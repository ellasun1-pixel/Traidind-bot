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
    sell_pct: float = 0.0
    sell_quantity: float = 0.0
    keep_quantity: float = 0.0
    trailing_stop_price: float = 0.0
    position_quantity: float = 0.0
    profit_pct: float = 0.0


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
        available_cash: float | None = None,
        active_mode: str = "PAPER_CHALLENGE",
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

        if active_mode == "LIVE_FUNDED":
            live_signal = self._check_live_funded_exits(
                symbol, regime, current_price, existing, portfolio_balance,
            )
            if live_signal:
                return live_signal
        else:
            tp_signal = self._check_take_profit(
                symbol, regime, current_price, existing, portfolio_balance
            )
            if tp_signal:
                return tp_signal

        cash = available_cash if available_cash is not None else portfolio_balance
        buy_signal = self._check_buy_conditions(
            symbol, regime, latest, prev, current_price, daily,
            portfolio_balance, existing, open_positions, total_open_risk_usd,
            cash,
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

        move_to_usd_level = settings.loss_level + 15
        if balance < move_to_usd_level:
            return TradeSignal(
                signal_type="MOVE_TO_USD",
                priority="CRITICAL",
                asset_symbol=symbol,
                regime=regime,
                entry_price=current_price,
                reason=f"Balance below ${move_to_usd_level:.0f} — move to USD recommended",
                explanation="Strongly recommended to go fully to cash to avoid defeat",
                current_balance=balance,
                distance_to_win=settings.win_level - balance,
                distance_to_loss=balance - settings.loss_level,
            )

        no_buy_level = settings.loss_level + 25
        if balance < no_buy_level:
            return TradeSignal(
                signal_type="SELL",
                priority="CRITICAL",
                asset_symbol=symbol,
                regime=regime,
                entry_price=current_price,
                reason="Balance dangerously close to loss level — exit all risk",
                explanation=f"Your balance is near ${settings.loss_level:.0f} defeat — sell to protect remaining capital",
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

    def _check_live_funded_exits(
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
            quantity = pos.get("quantity", 0)
            peak_price = pos.get("peak_price", current_price)
            tp1_fired = pos.get("tp1_fired", False)
            tp2_fired = pos.get("tp2_fired", False)

            if entry <= 0 or quantity <= 0:
                continue

            profit_pct = (current_price - entry) / entry
            position_value = quantity * current_price

            if not tp1_fired and profit_pct >= settings.live_tp1_pct:
                sell_qty = round(quantity * settings.live_tp1_sell_pct, 8)
                keep_qty = round(quantity - sell_qty, 8)
                sell_usd = sell_qty * current_price
                return TradeSignal(
                    signal_type="REDUCE",
                    priority="HIGH",
                    asset_symbol=symbol,
                    regime=regime,
                    entry_price=current_price,
                    position_size_usd=round(sell_usd, 2),
                    reason=f"Profit +{profit_pct*100:.1f}% — partial take-profit level 1",
                    explanation=(
                        f"Price rose {profit_pct*100:.1f}% from entry ${entry:.2f}. "
                        f"Sell {settings.live_tp1_sell_pct*100:.0f}% to lock in gains, "
                        f"keep {keep_qty:.6f} {symbol.split('/')[0]} running"
                    ),
                    current_balance=balance,
                    distance_to_win=round(settings.win_level - balance, 2),
                    distance_to_loss=round(balance - settings.loss_level, 2),
                    sell_pct=settings.live_tp1_sell_pct,
                    sell_quantity=sell_qty,
                    keep_quantity=keep_qty,
                    position_quantity=quantity,
                    profit_pct=round(profit_pct * 100, 1),
                )

            if tp1_fired and not tp2_fired and profit_pct >= settings.live_tp2_pct:
                sell_qty = round(quantity * settings.live_tp2_sell_pct, 8)
                keep_qty = round(quantity - sell_qty, 8)
                sell_usd = sell_qty * current_price
                return TradeSignal(
                    signal_type="REDUCE",
                    priority="HIGH",
                    asset_symbol=symbol,
                    regime=regime,
                    entry_price=current_price,
                    position_size_usd=round(sell_usd, 2),
                    reason=f"Profit +{profit_pct*100:.1f}% — partial take-profit level 2",
                    explanation=(
                        f"Price rose {profit_pct*100:.1f}% from entry ${entry:.2f}. "
                        f"Sell another {settings.live_tp2_sell_pct*100:.0f}% to lock in more gains, "
                        f"keep {keep_qty:.6f} {symbol.split('/')[0]} with trailing stop"
                    ),
                    current_balance=balance,
                    distance_to_win=round(settings.win_level - balance, 2),
                    distance_to_loss=round(balance - settings.loss_level, 2),
                    sell_pct=settings.live_tp2_sell_pct,
                    sell_quantity=sell_qty,
                    keep_quantity=keep_qty,
                    position_quantity=quantity,
                    profit_pct=round(profit_pct * 100, 1),
                )

            if profit_pct >= settings.live_trailing_activate_pct and peak_price > 0:
                trailing_stop = peak_price * (1 - settings.live_trailing_stop_pct)
                if current_price <= trailing_stop:
                    return TradeSignal(
                        signal_type="SELL",
                        priority="CRITICAL",
                        asset_symbol=symbol,
                        regime=regime,
                        entry_price=current_price,
                        reason=(
                            f"Trailing stop hit — peak ${peak_price:.2f}, "
                            f"stop ${trailing_stop:.2f}, current ${current_price:.2f}"
                        ),
                        explanation=(
                            f"Price dropped {settings.live_trailing_stop_pct*100:.0f}% "
                            f"from peak ${peak_price:.2f}. Sell remaining position to protect profits"
                        ),
                        current_balance=balance,
                        distance_to_win=round(settings.win_level - balance, 2),
                        distance_to_loss=round(balance - settings.loss_level, 2),
                        trailing_stop_price=round(trailing_stop, 2),
                        position_quantity=quantity,
                        profit_pct=round(profit_pct * 100, 1),
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
        available_cash: float | None = None,
    ) -> Optional[TradeSignal]:
        if regime == MarketRegime.PANIC:
            return None

        preserve_level = settings.win_level - 10
        critical_level = settings.loss_level + 5
        no_buy_level = settings.loss_level + 25
        if balance >= preserve_level:
            return None
        if balance <= critical_level:
            return None
        if balance < no_buy_level:
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

        if regime != MarketRegime.TREND:
            return None

        momentum_warning = self._check_momentum(latest, current_price)
        if momentum_warning == "BLOCK":
            logger.info(
                "BUY_BLOCKED_MOMENTUM %s: price falling despite TREND regime",
                symbol,
            )
            return None

        stop_distance_pct = 0.03
        expected_profit_pct = stop_distance_pct * self.take_profit_multiple
        if expected_profit_pct <= min_expected_profit_pct:
            return None
        risk_pct = settings.risk_per_trade_pct_default

        risk_dollars = settings.starting_balance * risk_pct
        max_total_risk = settings.starting_balance * settings.max_total_open_risk_pct
        if total_open_risk_usd + risk_dollars > max_total_risk:
            return None

        position_value = risk_dollars / stop_distance_pct
        stop_loss_price = current_price * (1 - stop_distance_pct)

        cash = available_cash if available_cash is not None else balance

        near_win_level = settings.win_level - 30
        mid_level = settings.starting_balance + 50
        if balance >= near_win_level:
            if regime != MarketRegime.TREND:
                return None
            er20 = latest.get("er20", 0) or 0
            adx_val = latest.get("adx14", 0) or 0
            if er20 < 0.5 and adx_val < 25:
                return None
            position_value = min(position_value, balance * 0.10)

        if balance < mid_level:
            max_invested = balance * 0.50
            position_value = min(position_value, max_invested)

        position_value = min(position_value, cash)
        if position_value < 10:
            return None

        price_range_low = current_price * 0.998
        price_range_high = current_price * 1.002

        reason = f"Regime={regime.value}, EMA200 trend confirmed, risk within budget"
        explanation = "Trend looks favorable and risk is managed — consider a small position"

        if momentum_warning:
            reason = f"{reason} | {momentum_warning}"
            explanation = f"{explanation}\n⚠️ {momentum_warning}"

        return TradeSignal(
            signal_type="BUY",
            priority="MEDIUM" if momentum_warning else "HIGH",
            asset_symbol=symbol,
            regime=regime,
            entry_price=current_price,
            stop_loss=stop_loss_price,
            position_size_usd=round(position_value, 2),
            max_loss_usd=round(risk_dollars, 2),
            order_type="LIMIT",
            cancel_level=round(current_price * 1.01, 2),
            reason=reason,
            explanation=explanation,
            price_range_low=round(price_range_low, 2),
            price_range_high=round(price_range_high, 2),
            remaining_usd=round(cash - position_value, 2),
            current_balance=balance,
            distance_to_win=round(settings.win_level - balance, 2),
            distance_to_loss=round(balance - settings.loss_level, 2),
        )

    def _check_momentum(self, latest: pd.Series, current_price: float) -> str | None:
        """Check short-term momentum against the TREND regime.

        Uses BOTH daily candle data AND live current_price vs last close
        to catch intraday drops that daily candles miss.

        Returns:
            "BLOCK" — price is actively crashing, suppress BUY entirely.
            A warning string — mild decline, let BUY through but warn user.
            None — momentum is fine, no warning needed.
        """
        change_1d = float(latest.get("price_change_short", 0) or 0)
        change_3d = float(latest.get("price_change_3d", 0) or 0)
        last_close = float(latest.get("close", 0) or 0)
        ema5 = float(latest.get("ema5", 0) or 0)

        intraday_change = 0.0
        if last_close > 0 and current_price > 0:
            intraday_change = (current_price - last_close) / last_close

        if change_3d <= -0.08:
            return "BLOCK"

        if change_1d <= -0.04:
            return "BLOCK"

        if intraday_change <= -0.04:
            return "BLOCK"

        warnings = []

        if intraday_change < -0.015:
            warnings.append(
                f"live price ${current_price:.2f} is {abs(intraday_change)*100:.1f}% "
                f"below last close ${last_close:.2f}"
            )

        if change_1d < -0.02:
            warnings.append(f"prev candle fell {abs(change_1d)*100:.1f}%")

        if change_3d < -0.04:
            warnings.append(f"fell {abs(change_3d)*100:.1f}% over 3 days")

        if ema5 > 0 and current_price < ema5:
            warnings.append(f"live price below 5-day EMA (${ema5:.2f})")

        if warnings:
            return "Short-term decline despite TREND: " + ", ".join(warnings)

        return None

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
