"""Tests for Phase 2: LIVE_FUNDED partial profit-taking and trailing stop.

Verifies:
(a) TP1 fires at +15% profit, sells 30%
(b) TP2 fires at +40% profit (after TP1), sells 30% of remaining
(c) Trailing stop activates at +15%, triggers sell at peak * (1-12%)
(d) PAPER_CHALLENGE mode does NOT use live funded exits
(e) Peak price tracking works correctly
(f) confirm_partial_sell marks tp_level correctly
(g) Settings are configurable (not hardcoded)
(h) Example notification format with real ZEC numbers
"""
from __future__ import annotations

import pytest
import pandas as pd

from src.config import settings
from src.strategy.engine import StrategyEngine, TradeSignal
from src.strategy.regime import MarketRegime
from src.portfolio.manager import PaperPortfolio, Position
from src.notifier.formatter import SignalFormatter


def _make_daily_df(price: float = 100.0, rows: int = 210) -> pd.DataFrame:
    """Minimal OHLCV daily data with enough rows for indicators."""
    close = [price * (1 + 0.001 * i) for i in range(rows)]
    return pd.DataFrame({
        "open": close,
        "high": [c * 1.01 for c in close],
        "low": [c * 0.99 for c in close],
        "close": close,
        "volume": [1000.0] * rows,
    })


def _position_dict(
    symbol="ZEC/USD", entry_price=30.0, quantity=12.31281,
    stop_loss=27.0, peak_price=None, tp1_fired=False, tp2_fired=False,
) -> dict:
    return {
        "symbol": symbol,
        "entry_price": entry_price,
        "quantity": quantity,
        "stop_loss": stop_loss,
        "position_value_usd": entry_price * quantity,
        "risk_per_unit": abs(entry_price - stop_loss),
        "peak_price": peak_price or entry_price,
        "tp1_fired": tp1_fired,
        "tp2_fired": tp2_fired,
    }


class TestConfigurableSettings:
    def test_tp1_pct_default(self):
        assert settings.live_tp1_pct == 0.15

    def test_tp1_sell_pct_default(self):
        assert settings.live_tp1_sell_pct == 0.30

    def test_tp2_pct_default(self):
        assert settings.live_tp2_pct == 0.40

    def test_tp2_sell_pct_default(self):
        assert settings.live_tp2_sell_pct == 0.30

    def test_trailing_stop_pct_default(self):
        assert settings.live_trailing_stop_pct == 0.12

    def test_trailing_activate_pct_default(self):
        assert settings.live_trailing_activate_pct == 0.15


class TestTP1Fires:
    def test_tp1_at_15pct_profit(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.16  # +16% > 15% threshold
        pos = _position_dict(entry_price=entry)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        assert signal is not None
        assert signal.signal_type == "REDUCE"
        assert "level 1" in signal.reason
        assert signal.sell_pct == settings.live_tp1_sell_pct

    def test_tp1_not_fired_below_threshold(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.10  # +10% < 15%
        pos = _position_dict(entry_price=entry)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        assert signal is None

    def test_tp1_sell_quantity_is_30pct(self):
        engine = StrategyEngine()
        entry = 30.0
        quantity = 12.31281
        current = entry * 1.20
        pos = _position_dict(entry_price=entry, quantity=quantity)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        expected_sell = round(quantity * 0.30, 8)
        expected_keep = round(quantity - expected_sell, 8)
        assert signal.sell_quantity == expected_sell
        assert signal.keep_quantity == expected_keep
        assert signal.position_quantity == quantity

    def test_tp1_not_retriggered_after_fired(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.20
        pos = _position_dict(entry_price=entry, tp1_fired=True)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        # At +20%, TP1 already fired, TP2 not yet (needs +40%) -> None
        assert signal is None


class TestTP2Fires:
    def test_tp2_at_40pct_profit(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.42  # +42% > 40%
        pos = _position_dict(entry_price=entry, tp1_fired=True)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        assert signal is not None
        assert signal.signal_type == "REDUCE"
        assert "level 2" in signal.reason
        assert signal.sell_pct == settings.live_tp2_sell_pct

    def test_tp2_not_fired_without_tp1(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.45  # +45% but TP1 not fired
        pos = _position_dict(entry_price=entry, tp1_fired=False)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        # Should fire TP1, not TP2
        assert signal is not None
        assert "level 1" in signal.reason

    def test_tp2_not_retriggered_after_fired(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.50
        pos = _position_dict(entry_price=entry, tp1_fired=True, tp2_fired=True)
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        # Both TP fired, trailing stop not hit -> None (price still above trailing)
        assert signal is None


class TestTrailingStop:
    def test_trailing_stop_triggers_on_drop(self):
        engine = StrategyEngine()
        entry = 30.0
        peak = entry * 1.50  # peak at +50%
        # Drop 13% from peak -> below 12% trailing threshold
        current = peak * 0.87
        pos = _position_dict(
            entry_price=entry, peak_price=peak,
            tp1_fired=True, tp2_fired=True,
        )
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        assert signal is not None
        assert signal.signal_type == "SELL"
        assert "Trailing stop" in signal.reason
        assert signal.trailing_stop_price > 0

    def test_trailing_stop_not_triggered_above_threshold(self):
        engine = StrategyEngine()
        entry = 30.0
        peak = entry * 1.50
        current = peak * 0.95  # 5% drop, well above 12% trailing
        pos = _position_dict(
            entry_price=entry, peak_price=peak,
            tp1_fired=True, tp2_fired=True,
        )
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        assert signal is None

    def test_trailing_stop_requires_activation_profit(self):
        engine = StrategyEngine()
        entry = 30.0
        peak = entry * 1.10  # only +10% peak, below 15% activation
        current = peak * 0.87  # big drop from peak
        pos = _position_dict(
            entry_price=entry, peak_price=peak,
            tp1_fired=False, tp2_fired=False,
        )
        signal = engine._check_live_funded_exits(
            "ZEC/USD", MarketRegime.TREND, current, [pos], 10000.0,
        )
        # Profit from entry is ~-4%, below activation -> no trailing
        assert signal is None


class TestPeakPriceTracking:
    def test_peak_price_updates_upward(self):
        pos = Position(
            symbol="ZEC/USD", side="BUY", entry_price=30.0,
            quantity=12.31281, position_value_usd=369.38,
            commission_usd=0.96, spread_cost_usd=0.37,
            stop_loss=27.0,
        )
        assert pos.peak_price == 30.0
        pos.update_peak_price(35.0)
        assert pos.peak_price == 35.0
        pos.update_peak_price(33.0)
        assert pos.peak_price == 35.0  # never moves down

    def test_portfolio_update_peak_prices(self):
        portfolio = PaperPortfolio(starting_balance=10000.0)
        pos = Position(
            symbol="ZEC/USD", side="BUY", entry_price=30.0,
            quantity=12.31281, position_value_usd=369.38,
            commission_usd=0.96, spread_cost_usd=0.37,
            stop_loss=27.0,
        )
        portfolio.positions.append(pos)
        portfolio.update_peak_prices({"ZEC/USD": 38.0})
        assert pos.peak_price == 38.0
        portfolio.update_peak_prices({"ZEC/USD": 36.0})
        assert pos.peak_price == 38.0


class TestConfirmPartialSellTPLevel:
    def _make_portfolio_with_position(self):
        portfolio = PaperPortfolio(starting_balance=10000.0)
        pos = Position(
            symbol="ZEC/USD", side="BUY", entry_price=30.0,
            quantity=12.31281, position_value_usd=369.38,
            commission_usd=0.96, spread_cost_usd=0.37,
            stop_loss=27.0,
        )
        portfolio.positions.append(pos)
        portfolio.balance_usd -= 370.71
        return portfolio

    def test_tp_level_1_marks_tp1_fired(self):
        portfolio = self._make_portfolio_with_position()
        ok, msg = portfolio.confirm_partial_sell("ZEC/USD", 34.5, 0.30, tp_level=1)
        assert ok
        pos = portfolio.positions[0]
        assert pos.tp1_fired is True
        assert pos.tp2_fired is False

    def test_tp_level_2_marks_tp2_fired(self):
        portfolio = self._make_portfolio_with_position()
        portfolio.positions[0].tp1_fired = True
        ok, msg = portfolio.confirm_partial_sell("ZEC/USD", 42.0, 0.30, tp_level=2)
        assert ok
        pos = portfolio.positions[0]
        assert pos.tp2_fired is True

    def test_partial_sell_reduces_quantity(self):
        portfolio = self._make_portfolio_with_position()
        original_qty = portfolio.positions[0].quantity
        ok, _ = portfolio.confirm_partial_sell("ZEC/USD", 34.5, 0.30, tp_level=1)
        assert ok
        assert portfolio.positions[0].quantity == pytest.approx(
            original_qty * 0.70, abs=0.001
        )

    def test_partial_sell_increases_balance(self):
        portfolio = self._make_portfolio_with_position()
        balance_before = portfolio.balance_usd
        ok, _ = portfolio.confirm_partial_sell("ZEC/USD", 34.5, 0.30, tp_level=1)
        assert ok
        assert portfolio.balance_usd > balance_before


class TestModeIsolation:
    def test_paper_challenge_uses_standard_tp(self):
        engine = StrategyEngine()
        entry = 30.0
        current = entry * 1.20  # +20% profit
        pos = _position_dict(entry_price=entry)
        # In PAPER_CHALLENGE mode, _check_live_funded_exits is NOT called
        # Instead _check_take_profit is used. We verify the mode branch:
        daily_df = _make_daily_df(price=current)
        h4_df = daily_df.copy()
        signal = engine.analyze(
            "ZEC/USD", daily_df, h4_df, current, 10000.0,
            [pos], 0.0, available_cash=9000.0,
            active_mode="PAPER_CHALLENGE",
        )
        # Should NOT produce a REDUCE signal (that's LIVE_FUNDED only)
        assert signal.signal_type != "REDUCE" or "level 1" not in signal.reason


class TestNotificationFormat:
    """Test notification formatting for REDUCE signals with real ZEC numbers."""

    def test_reduce_signal_shows_all_fields(self):
        formatter = SignalFormatter(beginner_mode=False)
        signal = TradeSignal(
            signal_type="REDUCE",
            priority="HIGH",
            asset_symbol="ZEC/USD",
            regime=MarketRegime.TREND,
            entry_price=34.50,
            position_size_usd=127.69,
            reason="Profit +15.0% — partial take-profit level 1",
            explanation=(
                "Price rose 15.0% from entry $30.00. "
                "Sell 30% to lock in gains, "
                "keep 8.618967 ZEC running"
            ),
            current_balance=10000.0,
            distance_to_win=1200.0,
            distance_to_loss=500.0,
            sell_pct=0.30,
            sell_quantity=3.693843,
            keep_quantity=8.618967,
            position_quantity=12.31281,
            profit_pct=15.0,
        )
        msg = formatter.format_signal(signal)
        assert "REDUCE" in msg
        assert "ZEC/USD" in msg
        assert "12.31281" in msg or "12.312810" in msg
        assert "3.693843" in msg
        assert "8.618967" in msg
        assert "30%" in msg
        assert "127.69" in msg or "127" in msg
        assert "15.0%" in msg or "+15.0%" in msg

    def test_example_zec_tp1_notification(self):
        """Real example: 12.31281 ZEC at $30 entry, price hits $34.50 (+15%)."""
        formatter = SignalFormatter(beginner_mode=True)
        entry = 30.0
        quantity = 12.31281
        current = 34.50
        profit_pct = (current - entry) / entry * 100
        sell_qty = round(quantity * 0.30, 6)
        keep_qty = round(quantity - sell_qty, 6)
        sell_usd = round(sell_qty * current, 2)

        signal = TradeSignal(
            signal_type="REDUCE",
            priority="HIGH",
            asset_symbol="ZEC/USD",
            regime=MarketRegime.TREND,
            entry_price=current,
            position_size_usd=sell_usd,
            reason=f"Profit +{profit_pct:.1f}% — partial take-profit level 1",
            explanation=(
                f"Price rose {profit_pct:.1f}% from entry ${entry:.2f}. "
                f"Sell 30% to lock in gains, keep {keep_qty:.6f} ZEC running"
            ),
            current_balance=10000.0,
            distance_to_win=1200.0,
            distance_to_loss=500.0,
            sell_pct=0.30,
            sell_quantity=sell_qty,
            keep_quantity=keep_qty,
            position_quantity=quantity,
            profit_pct=round(profit_pct, 1),
        )
        msg = formatter.format_signal(signal)
        assert "REDUCE" in msg
        assert "ZEC" in msg
        assert "/confirm" in msg
        assert "/reject" in msg

    def test_trailing_stop_notification(self):
        formatter = SignalFormatter(beginner_mode=False)
        signal = TradeSignal(
            signal_type="SELL",
            priority="CRITICAL",
            asset_symbol="ZEC/USD",
            regime=MarketRegime.TREND,
            entry_price=39.60,
            reason="Trailing stop hit — peak $45.00, stop $39.60, current $39.50",
            explanation="Price dropped 12% from peak $45.00. Sell remaining position to protect profits",
            current_balance=10000.0,
            distance_to_win=1200.0,
            distance_to_loss=500.0,
            trailing_stop_price=39.60,
            position_quantity=5.737,
            profit_pct=31.7,
        )
        msg = formatter.format_signal(signal)
        assert "SELL" in msg
        assert "Trailing stop" in msg
        assert "$45.00" in msg
        assert "/confirm" in msg
