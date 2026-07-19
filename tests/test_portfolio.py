import pytest
from src.portfolio.manager import PaperPortfolio
from src.config import settings
from src.strategy.engine import StrategyEngine


@pytest.fixture
def portfolio():
    return PaperPortfolio(starting_balance=1000.0)


class TestPnLCalculation:
    def test_realized_pnl_profit(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=52000.0)
        assert ok
        assert portfolio.realized_pnl_total > 0

    def test_realized_pnl_loss(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=48000.0)
        assert ok
        assert portfolio.realized_pnl_total < 0

    def test_unrealized_pnl(self, portfolio):
        portfolio.confirm_buy(
            symbol="ETH/USD",
            entry_price=3000.0,
            position_value_usd=100.0,
            stop_loss=2900.0,
            risk_dollars=3.0,
        )
        pnl = portfolio.get_unrealized_pnl({"ETH/USD": 3100.0})
        quantity = 100.0 / 3000.0
        commission = 100.0 * settings.commission_pct
        spread = 100.0 * settings.spread_pct
        expected = (3100.0 - 3000.0) * quantity - commission - spread
        assert abs(pnl - expected) < 0.01


class TestCommissionAndSpread:
    def test_commission_deducted_on_buy(self, portfolio):
        initial = portfolio.balance_usd
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        expected_cost = 100.0 + 100.0 * settings.commission_pct + 100.0 * settings.spread_pct
        assert abs(portfolio.balance_usd - (initial - expected_cost)) < 0.01

    def test_commission_deducted_on_sell(self, portfolio):
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        portfolio.confirm_sell("BTC/USD", exit_price=50000.0)
        assert portfolio.realized_pnl_total < 0


class TestConfirmTrade:
    def test_trade_only_after_confirm(self, portfolio):
        assert len(portfolio.get_open_positions()) == 0
        portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        assert len(portfolio.get_open_positions()) == 1

    def test_sell_no_position(self, portfolio):
        ok, msg = portfolio.confirm_sell("BTC/USD", exit_price=50000.0)
        assert not ok

    def test_challenge_won_blocks_new_trades(self, portfolio):
        portfolio.challenge_status = "won"
        ok, msg = portfolio.confirm_buy(
            symbol="BTC/USD",
            entry_price=50000.0,
            position_value_usd=100.0,
            stop_loss=48500.0,
            risk_dollars=3.0,
        )
        assert not ok


class TestRiskBudgetEnforcement:
    def test_exceeds_max_positions(self):
        p = PaperPortfolio(starting_balance=1100.0)
        ok1, _ = p.confirm_buy("BTC/USD", 50000, 30, 48500, 1.5)
        ok2, _ = p.confirm_buy("ETH/USD", 3000, 30, 2900, 1.5)
        assert ok1 and ok2
        ok, msg = p.confirm_buy("LTC/USD", 200, 30, 190, 1.5)
        assert not ok
        assert "positions" in msg.lower()

    def test_exceeds_risk_budget(self, portfolio):
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 16.0)
        assert not ok

    def test_within_new_risk_budget(self, portfolio):
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 400, 48500, 13.0)
        assert ok


class TestChallengeStatus:
    def test_win_at_1120(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        assert portfolio.challenge_status == "won"

    def test_loss_at_950(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        assert portfolio.challenge_status == "lost"

    def test_win_blocks_normal_buys(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert not ok

    def test_loss_blocks_buys(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert not ok

    def test_buy_does_not_falsely_trigger_loss(self):
        """Buying a position reduces cash but not equity — should stay active."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        assert ok
        assert portfolio.challenge_status == "active", (
            f"Balance=${portfolio.balance_usd:.2f}, equity=${portfolio._get_equity_estimate():.2f}, "
            f"status={portfolio.challenge_status} — should be active"
        )

    def test_confirm_uses_equity_not_cash_for_circuit_breakers(self):
        """Second confirm must not be blocked by low cash when equity is healthy.

        Reproduces: first BUY drops cash below $955, but equity stays ~$1000.
        A second BUY should pass the circuit breaker (equity > $975), not be
        rejected with 'BLOCKED: Balance <= $955'.
        """
        portfolio = PaperPortfolio(starting_balance=1000.0)
        ok1, msg1 = portfolio.confirm_buy("BTC/USD", 50000, 400, 48500, 13.0)
        assert ok1, f"First buy should succeed: {msg1}"

        assert portfolio.balance_usd < 600, "Cash should be well below $955 after large buy"
        equity = portfolio._get_equity_estimate()
        assert equity > 975, f"Equity should be healthy (~$1000), got ${equity:.2f}"

        ok2, msg2 = portfolio.confirm_buy("LINK/USD", 15.0, 100, 14.5, 13.0)
        assert ok2, (
            f"Second buy should NOT be blocked by circuit breaker. "
            f"Cash=${portfolio.balance_usd:.2f}, Equity=${equity:.2f}, "
            f"Rejection: {msg2}"
        )

    def test_equity_based_loss_detection(self):
        """Loss only triggers when total equity (cash + positions) drops to $950."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.balance_usd = 940.0
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "lost"

    def test_lost_recovers_to_active(self):
        """Lost status recovers when equity returns above loss_level."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "lost"
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "active"

    def test_won_is_terminal(self):
        """Won status does not revert to active."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        portfolio._update_challenge_status()
        assert portfolio.challenge_status == "won"

    def test_reset_challenge_status(self):
        """Manual reset recalculates from current equity."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "lost"
        msg = portfolio.reset_challenge_status()
        assert portfolio.challenge_status == "active"
        assert "lost" in msg and "active" in msg

    def test_reset_challenge_preserves_won(self):
        """Manual reset cannot revert terminal 'won' status."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        msg = portfolio.reset_challenge_status()
        assert portfolio.challenge_status == "won"
        assert "cannot reset" in msg.lower()


class TestChallengeEndedStopsSignals:
    def test_is_challenge_active_true_when_active(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        assert portfolio.is_challenge_active is True

    def test_is_challenge_active_false_when_won(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        assert portfolio.is_challenge_active is False

    def test_is_challenge_active_false_when_lost(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        assert portfolio.is_challenge_active is False

    def test_challenge_ended_message_won(self):
        portfolio = PaperPortfolio(starting_balance=1120.0)
        msg = portfolio.get_challenge_ended_message()
        assert "WON" in msg
        assert "1120" in msg or "1,120" in msg

    def test_challenge_ended_message_lost(self):
        portfolio = PaperPortfolio(starting_balance=950.0)
        msg = portfolio.get_challenge_ended_message()
        assert "LOST" in msg


class TestNewChallenge:
    def test_new_challenge_resets_balance(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.balance_usd = 800.0
        portfolio.challenge_status = "lost"
        archive, msg = portfolio.start_new_challenge()
        assert portfolio.balance_usd == 1000.0
        assert portfolio.challenge_status == "active"
        assert portfolio.is_challenge_active is True

    def test_new_challenge_archives_trades(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)
        portfolio.confirm_sell("BTC/USD", 51000)
        assert len(portfolio.closed_trades) == 1
        archive, msg = portfolio.start_new_challenge()
        assert archive["total_trades"] == 1
        assert len(archive["trades"]) == 1
        assert portfolio.closed_trades == []
        assert portfolio.positions == []

    def test_new_challenge_clears_realized_pnl(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.realized_pnl_total = 50.0
        archive, msg = portfolio.start_new_challenge()
        assert archive["realized_pnl"] == 50.0
        assert portfolio.realized_pnl_total == 0.0

    def test_new_challenge_message_format(self):
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.challenge_status = "won"
        archive, msg = portfolio.start_new_challenge()
        assert "New Paper Challenge started" in msg
        assert "WON" in msg
        assert "$1000.00" in msg or "$1,000.00" in msg


class TestEquityBasedDistances:
    def test_distance_to_win_uses_equity_not_cash(self):
        """After a BUY, distance_to_win should be based on equity (unchanged),
        not on reduced cash balance."""
        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)

        equity = portfolio._get_equity_estimate()
        assert abs(equity - 1000.0) < 1.0, "Equity should be ~$1000 after buy"
        assert portfolio.balance_usd < 900, "Cash should be reduced by ~$100"

        expected_distance_to_win = settings.win_level - equity
        expected_distance_to_loss = equity - settings.loss_level

        assert abs(expected_distance_to_win - 120.0) < 1.0
        assert abs(expected_distance_to_loss - 50.0) < 1.0

    def test_engine_receives_equity_based_distance(self):
        """The StrategyEngine should produce distance_to_win based on equity."""
        import pandas as pd
        import numpy as np

        portfolio = PaperPortfolio(starting_balance=1000.0)
        portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 3.0)

        equity = portfolio._get_equity_estimate()
        engine = StrategyEngine()

        df = pd.DataFrame({
            "open": np.random.uniform(49000, 51000, 250),
            "high": np.random.uniform(49000, 51000, 250),
            "low": np.random.uniform(49000, 51000, 250),
            "close": np.random.uniform(49000, 51000, 250),
            "volume": np.random.uniform(100, 1000, 250),
        })

        signal = engine.analyze(
            "BTC/USD", df, df, 50000.0,
            equity, portfolio.get_open_positions(),
            portfolio.get_total_open_risk(),
        )

        assert abs(signal.distance_to_win - (settings.win_level - equity)) < 0.01
        assert abs(signal.distance_to_loss - (equity - settings.loss_level)) < 0.01
