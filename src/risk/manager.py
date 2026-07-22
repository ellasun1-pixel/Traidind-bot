from __future__ import annotations

import logging
from src.config import settings

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self.starting_balance = settings.starting_balance
        self.win_level = settings.win_level
        self.loss_level = settings.loss_level

    def check_risk_budget(
        self,
        proposed_risk_usd: float,
        current_open_risk_usd: float,
        open_positions_count: int,
    ) -> tuple[bool, str]:
        max_total_risk = self.starting_balance * settings.max_total_open_risk_pct
        if current_open_risk_usd + proposed_risk_usd > max_total_risk:
            return False, (
                f"Total risk would exceed budget: "
                f"${current_open_risk_usd + proposed_risk_usd:.2f} > ${max_total_risk:.2f}"
            )

        if proposed_risk_usd > self.starting_balance * settings.risk_per_trade_pct_max:
            return False, (
                f"Risk per trade exceeds maximum: "
                f"${proposed_risk_usd:.2f} > ${self.starting_balance * settings.risk_per_trade_pct_max:.2f}"
            )

        if open_positions_count >= settings.max_open_positions:
            return False, f"Maximum open positions ({settings.max_open_positions}) reached"

        return True, ""

    def apply_circuit_breakers(
        self,
        balance: float,
        position_value_usd: float,
        signal_type: str,
    ) -> tuple[float, str]:
        if balance <= 955:
            if signal_type == "BUY":
                return 0.0, "BLOCKED: Balance ≤ $955 — no new trades allowed"
            return position_value_usd, "CRITICAL WARNING: Balance near defeat"

        if balance < 965:
            if signal_type == "BUY":
                return 0.0, "BLOCKED: Balance < $965 — MOVE TO USD recommended"
            return position_value_usd, ""

        if balance < 975:
            if signal_type == "BUY":
                return 0.0, "BLOCKED: Balance < $975 — no new buys, only risk reduction"
            return position_value_usd, ""

        if balance >= 1110:
            if signal_type == "BUY":
                return 0.0, "BLOCKED: Balance ≥ $1110 — preserve balance to reach $1120"
            return position_value_usd, ""

        if balance >= 1090:
            max_value = balance * 0.20
            adjusted = min(position_value_usd, max_value)
            note = "Near win: min 80% in USD" if adjusted < position_value_usd else ""
            return adjusted, note

        if balance >= 1050:
            max_value = balance * 0.50
            adjusted = min(position_value_usd, max_value)
            note = "Protect profit: max 50% deployed" if adjusted < position_value_usd else ""
            return adjusted, note

        if balance < 1050:
            max_value = balance * 0.50
            adjusted = min(position_value_usd, max_value)
            note = "Min 50% in USD required" if adjusted < position_value_usd else ""
            return adjusted, note

        return position_value_usd, ""

    def get_balance_status(self, balance: float) -> dict:
        return {
            "balance": balance,
            "distance_to_win": round(self.win_level - balance, 2),
            "distance_to_loss": round(balance - self.loss_level, 2),
            "challenge_status": self._challenge_status(balance),
            "circuit_breaker": self._active_breaker(balance),
        }

    def _challenge_status(self, balance: float) -> str:
        if balance >= self.win_level:
            return "WON"
        if balance <= self.loss_level:
            return "LOST"
        return "ACTIVE"

    def _active_breaker(self, balance: float) -> str:
        if balance <= 955:
            return "CRITICAL: No trades, near defeat"
        if balance < 965:
            return "MOVE_TO_USD recommended"
        if balance < 975:
            return "No new buys allowed"
        if balance < 1050:
            return "Min 50% cash"
        if balance >= 1110:
            return "Preserve — no new positions"
        if balance >= 1090:
            return "Strong signal only, 80% cash"
        if balance >= 1050:
            return "Protect profit, reduce risk"
        return "Normal"
