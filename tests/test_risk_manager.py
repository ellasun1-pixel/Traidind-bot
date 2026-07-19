import pytest
from src.risk.manager import RiskManager, PositionSizeResult
from src.config import settings


@pytest.fixture
def rm():
    return RiskManager()


class TestPositionSizing:
    def test_basic_position_size(self, rm):
        result = rm.calculate_position_size(
            risk_pct=0.003,
            entry_price=50000.0,
            stop_loss_price=48500.0,
        )
        assert result.approved
        expected_risk = 1000.0 * 0.003
        stop_dist = abs(50000 - 48500) / 50000
        expected_value = expected_risk / stop_dist
        assert abs(result.position_value_usd - expected_value) < 0.01
        assert abs(result.risk_dollars - expected_risk) < 0.01

    def test_position_size_formula(self, rm):
        result = rm.calculate_position_size(
            risk_pct=0.004,
            entry_price=100.0,
            stop_loss_price=97.0,
        )
        assert result.approved
        risk_dollars = 1000.0 * 0.004
        stop_distance = 3.0 / 100.0
        expected_value = risk_dollars / stop_distance
        assert abs(result.position_value_usd - round(expected_value, 2)) < 0.01
        assert abs(result.quantity - round(expected_value / 100.0, 8)) < 0.0001

    def test_zero_stop_distance(self, rm):
        result = rm.calculate_position_size(
            risk_pct=0.003,
            entry_price=100.0,
            stop_loss_price=100.0,
        )
        assert not result.approved

    def test_invalid_prices(self, rm):
        result = rm.calculate_position_size(
            risk_pct=0.003,
            entry_price=0,
            stop_loss_price=100.0,
        )
        assert not result.approved


class TestRiskBudget:
    def test_within_budget(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=13.0,
            current_open_risk_usd=12.0,
            current_balance=1000.0,
            open_positions_count=1,
        )
        assert ok

    def test_exceeds_total_risk(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=20.0,
            current_balance=1000.0,
            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_exceeds_per_trade_max(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=16.0,
            current_open_risk_usd=0.0,
            current_balance=1000.0,
            open_positions_count=0,
        )
        assert not ok
        assert "maximum" in reason.lower()

    def test_at_per_trade_max_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=0.0,
            current_balance=1000.0,
            open_positions_count=0,
        )
        assert ok

    def test_at_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=15.0,
            current_balance=1000.0,
            open_positions_count=1,
        )
        assert ok

    def test_just_over_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=15.01,
            current_balance=1000.0,
            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_max_positions_reached(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=12.0,
            current_open_risk_usd=0.0,
            current_balance=1000.0,
            open_positions_count=2,
        )
        assert not ok
        assert "positions" in reason.lower()


class TestCircuitBreakers:
    def test_balance_955_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(955, 100, "BUY")
        assert value == 0.0
        assert "BLOCKED" in note

    def test_balance_960_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(960, 100, "BUY")
        assert value == 0.0
        assert "BLOCKED" in note

    def test_balance_970_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(970, 100, "BUY")
        assert value == 0.0

    def test_balance_1110_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(1110, 100, "BUY")
        assert value == 0.0
        assert "preserve" in note.lower() or "BLOCKED" in note

    def test_balance_1090_limits_buy(self, rm):
        value, note = rm.apply_circuit_breakers(1095, 500, "BUY")
        assert value <= 1095 * 0.20

    def test_balance_below_1050_limits(self, rm):
        value, note = rm.apply_circuit_breakers(1020, 600, "BUY")
        assert value <= 1020 * 0.50

    def test_sell_allowed_at_low_balance(self, rm):
        value, note = rm.apply_circuit_breakers(960, 100, "SELL")
        assert value == 100

    def test_normal_balance(self, rm):
        value, note = rm.apply_circuit_breakers(1060, 100, "BUY")
        assert value == 100


class TestChallengeStatus:
    def test_won(self, rm):
        status = rm.get_balance_status(1120)
        assert status["challenge_status"] == "WON"

    def test_lost(self, rm):
        status = rm.get_balance_status(950)
        assert status["challenge_status"] == "LOST"

    def test_active(self, rm):
        status = rm.get_balance_status(1050)
        assert status["challenge_status"] == "ACTIVE"
