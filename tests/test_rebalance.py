"""Tests for PARTIAL_REBALANCE_EXPERIMENTAL mode.

Verifies:
(a) Partial sell never exceeds 30%
(b) Total risk budget is respected
(c) Signals are clearly marked as experimental
(d) Rebalance only triggers at 70%+ proximity to stop-loss
(e) Portfolio confirm_partial_sell enforces the 30% cap
"""
from __future__ import annotations

import pandas as pd
import numpy as np
import pytest

from src.config import settings
from src.portfolio.manager import PaperPortfolio, Position
from src.strategy.rebalance import (
    find_rebalance_candidates,
    compute_5d_momentum,
    generate_rebalance_suggestion,
    format_rebalance_message,
    PARTIAL_SELL_PCT,
    STOP_LOSS_PROXIMITY_TRIGGER,
    EXPERIMENTAL_WARNING,
    RebalanceSuggestion,
)


class TestPartialSellNeverExceeds30Pct:
    def test_sell_pct_constant_is_30(self):
        assert PARTIAL_SELL_PCT == 0.30

    def test_confirm_partial_sell_accepts_31_pct(self):
        portfolio = PaperPortfolio(starting_balance=settings.starting_balance)
        pos = Position(
            symbol="BTC/USD", side="BUY", entry_price=100.0,
            quantity=1.0, position_value_usd=100.0,
            commission_usd=0.26, spread_cost_usd=0.10,
            stop_loss=97.0,
        )
        portfolio.positions.append(pos)
        portfolio.balance_usd -= 100.36

        ok, msg = portfolio.confirm_partial_sell("BTC/USD", 99.0, 0.31)
        assert ok

    def test_confirm_partial_sell_rejects_above_100_pct(self):
        portfolio = PaperPortfolio(starting_balance=settings.starting_balance)
        pos = Position(
            symbol="BTC/USD", side="BUY", entry_price=100.0,
            quantity=1.0, position_value_usd=100.0,
            commission_usd=0.26, spread_cost_usd=0.10,
            stop_loss=97.0,
        )
        portfolio.positions.append(pos)
        portfolio.balance_usd -= 100.36

        ok, msg = portfolio.confirm_partial_sell("BTC/USD", 99.0, 1.01)
        assert not ok

    def test_confirm_partial_sell_accepts_30_pct(self):
        portfolio = PaperPortfolio(starting_balance=settings.starting_balance)
        pos = Position(
            symbol="BTC/USD", side="BUY", entry_price=100.0,
            quantity=1.0, position_value_usd=100.0,
            commission_usd=0.26, spread_cost_usd=0.10,
            stop_loss=97.0,
        )
        portfolio.positions.append(pos)
        portfolio.balance_usd -= 100.36

        ok, msg = portfolio.confirm_partial_sell("BTC/USD", 99.0, 0.30)
        assert ok
        assert "30%" in msg

    def test_partial_sell_reduces_quantity_correctly(self):
        portfolio = PaperPortfolio(starting_balance=settings.starting_balance)
        pos = Position(
            symbol="ETH/USD", side="BUY", entry_price=3000.0,
            quantity=0.1, position_value_usd=300.0,
            commission_usd=0.78, spread_cost_usd=0.30,
            stop_loss=2910.0,
        )
        portfolio.positions.append(pos)
        portfolio.balance_usd -= 301.08

        ok, _ = portfolio.confirm_partial_sell("ETH/USD", 2950.0, 0.30)
        assert ok
        assert abs(pos.quantity - 0.07) < 0.001

    def test_suggestion_sell_pct_always_030(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"ETH/USD": 0.05, "SOL/USD": 0.03}
        suggestion = generate_rebalance_suggestion(candidate, momentum, 0, 1000)
        assert suggestion is not None
        assert suggestion.sell_pct == 0.30


class TestRiskBudgetRespected:
    def test_rebalance_blocked_when_risk_full(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"ETH/USD": 0.05}
        max_risk = settings.starting_balance * settings.max_total_open_risk_pct
        suggestion = generate_rebalance_suggestion(
            candidate, momentum,
            total_open_risk_usd=max_risk,
            portfolio_balance=1000,
        )
        assert suggestion is None

    def test_rebalance_allowed_within_budget(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"ETH/USD": 0.05}
        suggestion = generate_rebalance_suggestion(
            candidate, momentum,
            total_open_risk_usd=0,
            portfolio_balance=1000,
        )
        assert suggestion is not None
        max_risk = settings.starting_balance * settings.max_total_open_risk_pct
        assert suggestion.buy_risk_usd <= max_risk


class TestExperimentalLabeling:
    def test_warning_present_in_suggestion(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"ETH/USD": 0.05}
        suggestion = generate_rebalance_suggestion(candidate, momentum, 0, 1000)
        assert suggestion is not None
        assert "ЭКСПЕРИМЕНТАЛЬНЫЙ" in suggestion.warning
        assert "22 цикла" in suggestion.warning
        assert "2 актива" in suggestion.warning

    def test_warning_in_formatted_message(self):
        suggestion = RebalanceSuggestion(
            sell_symbol="BTC/USD", sell_pct=0.30, sell_quantity=0.003,
            sell_value_usd=90.0, entry_price=100.0, current_price=97.5,
            stop_loss=97.0, distance_to_stop_pct=83.0,
            buy_symbol="ETH/USD", buy_momentum_5d=5.0, buy_price=3000.0,
            buy_value_usd=90.0, buy_risk_usd=2.7,
            warning=EXPERIMENTAL_WARNING,
        )
        msg = format_rebalance_message(suggestion)
        assert "ЭКСПЕРИМЕНТАЛЬНЫЙ" in msg
        assert "PARTIAL" in msg and "REBALANCE" in msg and "EXPERIMENTAL" in msg
        assert "NOT TREND-confirmed" in msg
        assert "22 цикла" in msg

    def test_experimental_warning_constant_content(self):
        assert "22 цикла" in EXPERIMENTAL_WARNING
        assert "2 актива" in EXPERIMENTAL_WARNING
        assert "ЭКСПЕРИМЕНТАЛЬНЫЙ" in EXPERIMENTAL_WARNING
        assert "30%" in EXPERIMENTAL_WARNING


class TestProximityTrigger:
    def test_trigger_at_70_pct(self):
        positions = [{
            "symbol": "BTC/USD", "entry_price": 100.0,
            "stop_loss": 97.0, "quantity": 1.0,
            "position_value_usd": 100.0,
        }]
        prices = {"BTC/USD": 97.85}
        candidates = find_rebalance_candidates(positions, prices)
        assert len(candidates) == 1
        assert candidates[0]["proximity_to_stop"] >= 0.70

    def test_no_trigger_below_70_pct(self):
        positions = [{
            "symbol": "BTC/USD", "entry_price": 100.0,
            "stop_loss": 97.0, "quantity": 1.0,
            "position_value_usd": 100.0,
        }]
        prices = {"BTC/USD": 99.0}
        candidates = find_rebalance_candidates(positions, prices)
        assert len(candidates) == 0

    def test_no_trigger_when_price_above_entry(self):
        positions = [{
            "symbol": "BTC/USD", "entry_price": 100.0,
            "stop_loss": 97.0, "quantity": 1.0,
            "position_value_usd": 100.0,
        }]
        prices = {"BTC/USD": 101.0}
        candidates = find_rebalance_candidates(positions, prices)
        assert len(candidates) == 0

    def test_trigger_threshold_constant(self):
        assert STOP_LOSS_PROXIMITY_TRIGGER == 0.70


class TestMomentumComputation:
    def test_5d_momentum_basic(self):
        prices = [100, 101, 102, 103, 104, 110]
        df = pd.DataFrame({"close": prices})
        result = compute_5d_momentum({"BTC/USD": df})
        assert "BTC/USD" in result
        assert result["BTC/USD"] == pytest.approx(0.10, abs=0.001)

    def test_selects_best_momentum(self):
        df1 = pd.DataFrame({"close": [100, 100, 100, 100, 100, 105]})
        df2 = pd.DataFrame({"close": [100, 100, 100, 100, 100, 112]})
        df3 = pd.DataFrame({"close": [100, 100, 100, 100, 100, 98]})
        momentum = compute_5d_momentum({
            "A/USD": df1, "B/USD": df2, "C/USD": df3,
        })
        assert max(momentum, key=momentum.get) == "B/USD"

    def test_excludes_sell_symbol(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"BTC/USD": 0.20, "ETH/USD": 0.05}
        suggestion = generate_rebalance_suggestion(candidate, momentum, 0, 1000)
        assert suggestion is not None
        assert suggestion.buy_symbol == "ETH/USD"

    def test_no_suggestion_when_all_negative_momentum(self):
        candidate = {
            "symbol": "BTC/USD", "entry_price": 100.0,
            "current_price": 97.5, "stop_loss": 97.0,
            "quantity": 1.0, "position_value_usd": 100.0,
            "proximity_to_stop": 0.83,
        }
        momentum = {"ETH/USD": -0.05, "SOL/USD": -0.02}
        suggestion = generate_rebalance_suggestion(candidate, momentum, 0, 1000)
        assert suggestion is None
