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
        critical_level = self.loss_level + 5
        move_to_usd_level = self.loss_level + 15
        no_buy_level = self.loss_level + 25
        preserve_level = self.win_level - 10
        near_win_level = self.win_level - 30
        mid_level = self.starting_balance + 50

        if balance <= critical_level:
            if signal_type == "BUY":
                return 0.0, f"BLOCKED: Balance ≤ ${critical_level:.0f} — no new trades allowed"
            return position_value_usd, "CRITICAL WARNING: Balance near defeat"

        if balance < move_to_usd_level:
            if signal_type == "BUY":
                return 0.0, f"BLOCKED: Balance < ${move_to_usd_level:.0f} — MOVE TO USD recommended"
            return position_value_usd, ""

        if balance < no_buy_level:
            if signal_type == "BUY":
                return 0.0, f"BLOCKED: Balance < ${no_buy_level:.0f} — no new buys, only risk reduction"
            return position_value_usd, ""

        if balance >= preserve_level:
            if signal_type == "BUY":
                return 0.0, f"BLOCKED: Balance ≥ ${preserve_level:.0f} — preserve balance to reach ${self.win_level:.0f}"
            return position_value_usd, ""

        if balance >= near_win_level:
            max_value = balance * 0.20
            adjusted = min(position_value_usd, max_value)
            note = "Near win: min 80% in USD" if adjusted < position_value_usd else ""
            return adjusted, note

        if balance >= mid_level:
            max_value = balance * 0.50
            adjusted = min(position_value_usd, max_value)
            note = "Protect profit: max 50% deployed" if adjusted < position_value_usd else ""
            return adjusted, note

        if balance < mid_level:
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
        critical_level = self.loss_level + 5
        move_to_usd_level = self.loss_level + 15
        no_buy_level = self.loss_level + 25
        mid_level = self.starting_balance + 50
        near_win_level = self.win_level - 30
        preserve_level = self.win_level - 10

        if balance <= critical_level:
            return "CRITICAL: No trades, near defeat"
        if balance < move_to_usd_level:
            return "MOVE_TO_USD recommended"
        if balance < no_buy_level:
            return "No new buys allowed"
        if balance < mid_level:
            return "Min 50% cash"
        if balance >= preserve_level:
            return "Preserve — no new positions"
        if balance >= near_win_level:
            return "Strong signal only, 80% cash"
        if balance >= mid_level:
            return "Protect profit, reduce risk"
        return "Normal"
