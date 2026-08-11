from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import settings
from src.strategy.indicators import compute_indicators

import pandas as pd

logger = logging.getLogger(__name__)

PARTIAL_SELL_PCT = 0.30
STOP_LOSS_PROXIMITY_TRIGGER = 0.70


@dataclass
class RebalanceSuggestion:
    sell_symbol: str
    sell_pct: float
    sell_quantity: float
    sell_value_usd: float
    entry_price: float
    current_price: float
    stop_loss: float
    distance_to_stop_pct: float

    buy_symbol: str
    buy_momentum_5d: float
    buy_price: float
    buy_value_usd: float
    buy_risk_usd: float

    warning: str


EXPERIMENTAL_WARNING = (
    "⚠️ ЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ"
    " — параметр 30% основан на ограниченном"
    " тесте (22 цикла, 2 актива),"
    " не такая же надёжная проверка,"
    " как основная TREND-стратегия."
    " Для тестирования и сравнения,"
    " не как основная рекомендация."
)


def find_rebalance_candidates(
    open_positions: list[dict],
    current_prices: dict[str, float],
) -> list[dict]:
    candidates = []
    for pos in open_positions:
        symbol = pos.get("symbol", "")
        entry = pos.get("entry_price", 0)
        stop = pos.get("stop_loss", 0)
        quantity = pos.get("quantity", 0)
        current = current_prices.get(symbol, 0)

        if entry <= 0 or stop <= 0 or current <= 0 or quantity <= 0:
            continue

        total_distance = entry - stop
        if total_distance <= 0:
            continue

        price_drop = entry - current
        if price_drop <= 0:
            continue

        proximity = price_drop / total_distance
        if proximity >= STOP_LOSS_PROXIMITY_TRIGGER:
            candidates.append({
                "symbol": symbol,
                "entry_price": entry,
                "stop_loss": stop,
                "current_price": current,
                "quantity": quantity,
                "position_value_usd": pos.get("position_value_usd", entry * quantity),
                "proximity_to_stop": round(proximity, 4),
            })

    return candidates


def compute_5d_momentum(daily_dfs: dict[str, pd.DataFrame]) -> dict[str, float]:
    momentum = {}
    for symbol, df in daily_dfs.items():
        if df is None or df.empty or len(df) < 6:
            continue
        close_col = df["close"] if "close" in df.columns else None
        if close_col is None:
            continue
        current = float(close_col.iloc[-1])
        past = float(close_col.iloc[-6])
        if past > 0:
            momentum[symbol] = (current - past) / past
    return momentum


def generate_rebalance_suggestion(
    candidate: dict,
    momentum: dict[str, float],
    total_open_risk_usd: float,
    portfolio_balance: float,
) -> RebalanceSuggestion | None:
    sell_symbol = candidate["symbol"]
    entry = candidate["entry_price"]
    current = candidate["current_price"]
    stop = candidate["stop_loss"]
    quantity = candidate["quantity"]

    sell_quantity = quantity * PARTIAL_SELL_PCT
    sell_value = sell_quantity * current

    eligible = {
        sym: mom for sym, mom in momentum.items()
        if sym != sell_symbol and mom > 0
    }
    if not eligible:
        return None

    best_symbol = max(eligible, key=eligible.get)
    best_momentum = eligible[best_symbol]
    best_price = 0.0

    buy_value = sell_value
    stop_distance_pct = 0.03
    buy_risk = buy_value * stop_distance_pct

    max_total_risk = settings.starting_balance * settings.max_total_open_risk_pct
    if total_open_risk_usd + buy_risk > max_total_risk:
        return None

    return RebalanceSuggestion(
        sell_symbol=sell_symbol,
        sell_pct=PARTIAL_SELL_PCT,
        sell_quantity=round(sell_quantity, 8),
        sell_value_usd=round(sell_value, 2),
        entry_price=entry,
        current_price=current,
        stop_loss=stop,
        distance_to_stop_pct=round(candidate["proximity_to_stop"] * 100, 1),
        buy_symbol=best_symbol,
        buy_momentum_5d=round(best_momentum * 100, 2),
        buy_price=best_price,
        buy_value_usd=round(buy_value, 2),
        buy_risk_usd=round(buy_risk, 2),
        warning=EXPERIMENTAL_WARNING,
    )


def format_rebalance_message(suggestion: RebalanceSuggestion) -> str:
    lines = [
        f"{suggestion.warning}",
        "",
        "*PARTIAL\\_REBALANCE\\_EXPERIMENTAL*",
        "",
        f"*Sell {suggestion.sell_pct*100:.0f}% of {suggestion.sell_symbol}:*",
        f"  Entry: ${suggestion.entry_price:,.2f}",
        f"  Current: ${suggestion.current_price:,.2f}",
        f"  Stop-loss: ${suggestion.stop_loss:,.2f}",
        f"  Distance to stop: {suggestion.distance_to_stop_pct:.0f}% reached",
        f"  Sell qty: {suggestion.sell_quantity:.6f}",
        f"  Sell value: ${suggestion.sell_value_usd:,.2f}",
        "",
        f"*Rotate into {suggestion.buy_symbol}:*",
        f"  5-day momentum: +{suggestion.buy_momentum_5d:.1f}%",
        f"  Buy value: ${suggestion.buy_value_usd:,.2f}",
        f"  Risk: ${suggestion.buy_risk_usd:,.2f}",
        f"  ⚠️ NOT TREND-confirmed — momentum-only signal",
        "",
        "Remaining 70% of original position holds at original stop/TP.",
        "",
        "Use /confirm to execute, or ignore to skip.",
    ]
    return "\n".join(lines)
