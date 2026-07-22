import pytest
from src.risk.manager import RiskManager
from src.config import settings


@pytest.fixture
def rm():
    return RiskManager()


class TestDeadCodeRemoved:
    """Fix #16: calculate_position_size and PositionSizeResult should not exist."""

    def test_no_calculate_position_size(self, rm):
        assert not hasattr(rm, "calculate_position_size"), (
            "calculate_position_size is dead code — the engine computes position size inline"
        )

    def test_no_position_size_result_class(self):
        import src.risk.manager as mod
        assert not hasattr(mod, "PositionSizeResult"), (
            "PositionSizeResult is dead code — removed with calculate_position_size"
        )


class TestRiskBudget:
    def test_within_budget(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=13.0,
            current_open_risk_usd=12.0,

            open_positions_count=1,
        )
        assert ok

    def test_exceeds_total_risk(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=20.0,

            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_exceeds_per_trade_max(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=16.0,
            current_open_risk_usd=0.0,

            open_positions_count=0,
        )
        assert not ok
        assert "maximum" in reason.lower()

    def test_at_per_trade_max_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=0.0,

            open_positions_count=0,
        )
        assert ok

    def test_at_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=15.0,

            open_positions_count=1,
        )
        assert ok

    def test_just_over_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=15.0,
            current_open_risk_usd=15.01,

            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_max_positions_reached(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=12.0,
            current_open_risk_usd=0.0,

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
        assert value == 100  # $100 is under 50% of $1060, so passes through

    def test_circuit_breaker_1050_to_1089_caps_at_50_pct(self, rm):
        value, note = rm.apply_circuit_breakers(1070, 600, "BUY")
        assert value == 1070 * 0.50
        assert "50%" in note

    def test_circuit_breaker_1050_to_1089_no_cap_if_under(self, rm):
        value, note = rm.apply_circuit_breakers(1070, 100, "BUY")
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
