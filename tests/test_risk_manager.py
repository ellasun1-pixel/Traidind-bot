import pytest
from src.risk.manager import RiskManager
from src.config import settings


@pytest.fixture
def rm():
    return RiskManager()


SB = settings.starting_balance
LL = settings.loss_level
WL = settings.win_level
MAX_TOTAL_RISK = SB * settings.max_total_open_risk_pct
PER_TRADE_MAX = SB * settings.risk_per_trade_pct_max


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
            proposed_risk_usd=MAX_TOTAL_RISK * 0.3,
            current_open_risk_usd=MAX_TOTAL_RISK * 0.3,
            open_positions_count=1,
        )
        assert ok

    def test_exceeds_total_risk(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=MAX_TOTAL_RISK * 0.6,
            current_open_risk_usd=MAX_TOTAL_RISK * 0.5,
            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_exceeds_per_trade_max(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=PER_TRADE_MAX + 10,
            current_open_risk_usd=0.0,
            open_positions_count=0,
        )
        assert not ok
        assert "maximum" in reason.lower()

    def test_at_per_trade_max_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=PER_TRADE_MAX,
            current_open_risk_usd=0.0,
            open_positions_count=0,
        )
        assert ok

    def test_at_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=MAX_TOTAL_RISK * 0.5,
            current_open_risk_usd=MAX_TOTAL_RISK * 0.5,
            open_positions_count=1,
        )
        assert ok

    def test_just_over_total_risk_boundary(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=MAX_TOTAL_RISK * 0.5,
            current_open_risk_usd=MAX_TOTAL_RISK * 0.5 + 0.01,
            open_positions_count=1,
        )
        assert not ok
        assert "budget" in reason.lower()

    def test_max_positions_reached(self, rm):
        ok, reason = rm.check_risk_budget(
            proposed_risk_usd=PER_TRADE_MAX * 0.5,
            current_open_risk_usd=0.0,
            open_positions_count=2,
        )
        assert not ok
        assert "positions" in reason.lower()


class TestCircuitBreakers:
    def test_balance_at_critical_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(LL + 5, 1000, "BUY")
        assert value == 0.0
        assert "BLOCKED" in note

    def test_balance_at_move_to_usd_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(LL + 10, 1000, "BUY")
        assert value == 0.0
        assert "BLOCKED" in note

    def test_balance_at_no_buy_level_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(LL + 20, 1000, "BUY")
        assert value == 0.0

    def test_balance_at_preserve_level_blocks_buy(self, rm):
        value, note = rm.apply_circuit_breakers(WL - 10, 1000, "BUY")
        assert value == 0.0
        assert "preserve" in note.lower() or "BLOCKED" in note

    def test_near_win_limits_buy(self, rm):
        balance = WL - 25
        value, note = rm.apply_circuit_breakers(balance, 5000, "BUY")
        assert value <= balance * 0.20

    def test_below_mid_level_limits(self, rm):
        balance = SB + 20
        value, note = rm.apply_circuit_breakers(balance, 6000, "BUY")
        assert value <= balance * 0.50

    def test_sell_allowed_at_low_balance(self, rm):
        value, note = rm.apply_circuit_breakers(LL + 10, 1000, "SELL")
        assert value == 1000

    def test_normal_balance(self, rm):
        balance = SB + 60
        value, note = rm.apply_circuit_breakers(balance, 1000, "BUY")
        assert value == 1000

    def test_circuit_breaker_mid_range_caps_at_50_pct(self, rm):
        balance = SB + 70
        value, note = rm.apply_circuit_breakers(balance, 6000, "BUY")
        assert value == balance * 0.50
        assert "50%" in note

    def test_circuit_breaker_mid_range_no_cap_if_under(self, rm):
        balance = SB + 70
        value, note = rm.apply_circuit_breakers(balance, 1000, "BUY")
        assert value == 1000


class TestChallengeStatus:
    def test_won(self, rm):
        status = rm.get_balance_status(WL)
        assert status["challenge_status"] == "WON"

    def test_lost(self, rm):
        status = rm.get_balance_status(LL)
        assert status["challenge_status"] == "LOST"

    def test_active(self, rm):
        status = rm.get_balance_status(SB + 50)
        assert status["challenge_status"] == "ACTIVE"
