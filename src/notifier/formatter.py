from __future__ import annotations

from src.config import settings
from src.strategy.engine import TradeSignal


BEGINNER_TERMS = {
    "STOP-LOSS": "automatic exit to limit losses",
    "LIMIT": "order at a specific price",
    "MARKET": "order at current price",
    "EMA200": "200-day average price — shows long-term trend",
    "Drawdown": "largest drop from peak balance",
    "P&L": "Profit and Loss",
    "TREND": "market moving in a clear direction",
    "CHOP": "market moving sideways without clear direction",
    "PANIC": "sudden market crash with extreme volatility",
    "LOWVOL": "very calm market with small price movements",
    "Circuit breaker": "automatic safety limit based on your balance",
}


class SignalFormatter:
    def __init__(self, beginner_mode: bool | None = None):
        self.beginner = beginner_mode if beginner_mode is not None else settings.beginner_explanations

    def format_signal(self, signal: TradeSignal) -> str:
        lines = [
            f"{'🔴' if signal.priority == 'CRITICAL' else '🟡' if signal.priority == 'HIGH' else '🔵'} "
            f"*{signal.priority}* Signal",
            "",
            f"📊 *Decision:* {signal.signal_type}",
            f"💰 *Asset:* {signal.asset_symbol}",
            f"💵 *Amount:* ${signal.position_size_usd:.2f}" if signal.position_size_usd else "",
            f"📈 *Price range:* ${signal.price_range_low:.2f} – ${signal.price_range_high:.2f}"
            if signal.price_range_low else "",
            f"📋 *Order type:* {self._term('LIMIT' if signal.order_type == 'LIMIT' else 'MARKET')}",
            f"🚫 *Cancel if price goes above:* ${signal.cancel_level:.2f}" if signal.cancel_level else "",
            f"🛑 *{self._term('STOP-LOSS')}:* ${signal.stop_loss:.2f}" if signal.stop_loss else "",
            f"⚠️ *Max possible loss:* ${signal.max_loss_usd:.2f}" if signal.max_loss_usd else "",
            "",
            f"💬 *{signal.explanation}*" if signal.explanation else "",
            f"📝 *Reason:* {signal.reason}",
            "",
            f"💰 *USD after trade:* ${signal.remaining_usd:.2f}" if signal.remaining_usd else "",
            f"🏦 *Current balance:* ${signal.current_balance:.2f}",
            f"🎯 *To win ($1120):* ${signal.distance_to_win:.2f}",
            f"⚠️ *To defeat ($950):* ${signal.distance_to_loss:.2f}",
        ]
        lines = [l for l in lines if l or l == ""]
        text = "\n".join(lines)

        if signal.signal_type in ("BUY", "SELL", "REDUCE", "TAKE_PROFIT", "MOVE_TO_USD"):
            if signal.signal_type == "BUY":
                text += "\n\n✅ Reply /confirm to execute | ❌ Reply /reject to skip"
            else:
                text += "\n\n✅ Reply /confirm to execute | ❌ Reply /reject to skip"

        return text

    def format_portfolio_summary(self, summary: dict) -> str:
        lines = [
            "📊 *Portfolio Status*",
            "",
            f"💰 Balance: ${summary['balance_usd']:.2f}",
            f"📈 Total Equity: ${summary['total_equity']:.2f}",
            f"📊 Unrealized {self._term('P&L')}: ${summary['unrealized_pnl']:.2f}",
            f"📊 Realized {self._term('P&L')}: ${summary['realized_pnl']:.2f}",
            f"📉 {self._term('Drawdown')}: {summary['drawdown_pct']:.2f}%",
            f"🏔️ Peak: ${summary['peak_balance']:.2f}",
            "",
            f"🎯 To win: ${summary['distance_to_win']:.2f}",
            f"⚠️ To defeat: ${summary['distance_to_loss']:.2f}",
            f"📊 Status: {summary['challenge_status'].upper()}",
            f"📂 Open positions: {summary['open_positions_count']}",
            f"📋 Total trades: {summary['total_trades']}",
        ]
        if summary.get("open_positions"):
            lines.append("")
            lines.append("*Open Positions:*")
            for pos in summary["open_positions"]:
                lines.append(
                    f"  • {pos['symbol']}: {pos['quantity']:.6f} @ ${pos['entry_price']:.2f} "
                    f"(stop: ${pos['stop_loss']:.2f})"
                )
        return "\n".join(lines)

    def _term(self, term: str) -> str:
        if self.beginner and term in BEGINNER_TERMS:
            return f"{term} ({BEGINNER_TERMS[term]})"
        return term
