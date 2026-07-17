import pytest
from src.portfolio.manager import PaperPortfolio
from src.config import settings


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
        ok, msg = portfolio.confirm_buy("BTC/USD", 50000, 100, 48500, 12.0)
        assert not ok


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
