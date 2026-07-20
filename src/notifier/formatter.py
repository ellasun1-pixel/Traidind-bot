from __future__ import annotations

from datetime import datetime, timezone

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


def _esc(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


class SignalFormatter:
    def __init__(self, beginner_mode: bool | None = None):
        self.beginner = beginner_mode if beginner_mode is not None else settings.beginner_explanations

    def format_signal(self, signal: TradeSignal, signal_id: str = None) -> str:
        if signal.signal_type in ("NO_TRADE", "WAIT"):
            return self._format_no_trade(signal)

        priority_emoji = {
            "CRITICAL": "\U0001f534",
            "HIGH": "\U0001f7e1",
            "MEDIUM": "\U0001f535",
        }
        emoji = priority_emoji.get(signal.priority, "\U0001f535")
        regime_val = signal.regime.value if hasattr(signal.regime, "value") else str(signal.regime)

        lines = [
            f"{emoji} *{signal.priority} Signal — {_esc(signal.signal_type)}*",
            "",
            "⚡ *PAPER TRADE — no real order will be placed.*",
            "",
        ]

        if signal_id:
            lines.append(f"Signal ID: `{signal_id}`")

        lines.append(f"Asset: {signal.asset_symbol}")
        lines.append(f"Action: {_esc(signal.signal_type)}")

        if signal.entry_price:
            lines.append(f"Current / Entry Price: ${signal.entry_price:.2f}")
        if signal.price_range_low and signal.price_range_high:
            lines.append(f"Entry Range: ${signal.price_range_low:.2f} – ${signal.price_range_high:.2f}")
        if signal.stop_loss:
            lines.append(f"{self._term('STOP-LOSS')}: ${signal.stop_loss:.2f}")
        if signal.entry_price and signal.stop_loss and signal.max_loss_usd:
            risk = abs(signal.entry_price - signal.stop_loss)
            if signal.stop_loss > 0 and risk > 0:
                take_profit = signal.entry_price + risk * settings.take_profit_risk_multiple
                rr = settings.take_profit_risk_multiple
                lines.append(f"Take Profit: ${take_profit:.2f}")
                lines.append(f"Risk/Reward: 1:{rr:.1f}")

        if signal.position_size_usd:
            lines.append(f"Suggested Size: ${signal.position_size_usd:.2f}")
        if signal.max_loss_usd:
            lines.append(f"Max Possible Loss: ${signal.max_loss_usd:.2f}")

        lines.append(f"Market Regime: {_esc(regime_val)}")
        lines.append(f"Order Type: {self._term(signal.order_type or 'LIMIT')}")
        if signal.cancel_level:
            lines.append(f"Cancel if price above: ${signal.cancel_level:.2f}")

        lines.append("")

        if signal.explanation:
            lines.append(f"_{_esc(signal.explanation)}_")
        if signal.reason:
            lines.append(f"Reason: {_esc(signal.reason)}")

        lines.append("")
        lines.append(f"Equity: ${signal.current_balance:.2f}")
        lines.append(f"To win ($1120): ${signal.distance_to_win:.2f}")
        lines.append(f"To defeat ($950): ${signal.distance_to_loss:.2f}")
        if signal.remaining_usd:
            lines.append(f"Cash after trade: ${signal.remaining_usd:.2f}")

        lines.append("")
        lines.append(f"Expires: {settings.signal_expiry_minutes} minutes")
        lines.append("")

        if signal.signal_type == "BUY":
            lines.append("_Signal invalidated if price moves above cancel level "
                         "or regime changes to PANIC._")
        elif signal.signal_type in ("SELL", "TAKE_PROFIT", "REDUCE", "MOVE_TO_USD"):
            lines.append("_Signal invalidated if position is already closed._")

        lines.append("")
        lines.append("✅ Reply /confirm to execute | ❌ Reply /reject to skip")

        return "\n".join(lines)

    def _format_no_trade(self, signal: TradeSignal) -> str:
        regime_val = signal.regime.value if hasattr(signal.regime, "value") else str(signal.regime)
        lines = [
            f"⏸ *{_esc(signal.signal_type)}* — {signal.asset_symbol}",
            "",
            "⚡ *PAPER TRADE — no real order will be placed.*",
            "",
            f"Market Regime: {_esc(regime_val)}",
            f"Reason: {_esc(signal.reason)}",
        ]
        if signal.explanation:
            lines.append(f"_{_esc(signal.explanation)}_")
        lines.append("")
        lines.append(f"Equity: ${signal.current_balance:.2f}")
        lines.append(f"To win: ${signal.distance_to_win:.2f} | To defeat: ${signal.distance_to_loss:.2f}")
        lines.append("")
        lines.append("_No action needed. The bot will continue monitoring._")
        return "\n".join(lines)

    def format_portfolio_summary(self, summary: dict) -> str:
        lines = [
            "\U0001f4ca *Portfolio Status*",
            "",
            f"\U0001f4b0 Equity: ${summary['total_equity']:.2f}",
            f"\U0001f4b5 Cash: ${summary['balance_usd']:.2f}",
            f"\U0001f4ca Unrealized {self._term('P&L')}: ${summary['unrealized_pnl']:.2f}",
            f"\U0001f4ca Realized {self._term('P&L')}: ${summary['realized_pnl']:.2f}",
            f"\U0001f4c9 {self._term('Drawdown')}: {summary['drawdown_pct']:.2f}%",
            f"\U0001f3d4️ Peak: ${summary['peak_balance']:.2f}",
            "",
            f"\U0001f3af To win: ${summary['distance_to_win']:.2f}",
            f"⚠️ To defeat: ${summary['distance_to_loss']:.2f}",
            f"\U0001f4ca Status: {summary['challenge_status'].upper()}",
            f"\U0001f4c2 Open positions: {summary['open_positions_count']}",
            f"\U0001f4cb Total trades: {summary['total_trades']}",
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

    def format_report(self, report_type: str, summary: dict,
                      health_status: str = "Unavailable",
                      last_signals: dict = None,
                      pending_signals: list = None,
                      scheduler_info: dict = None) -> str:
        if report_type == "morning":
            header = "☀️ *Morning Report*"
        else:
            header = "\U0001f319 *Evening Report*"

        starting = settings.starting_balance
        cash = summary["balance_usd"]
        equity = summary["total_equity"]
        total_return_pct = ((equity - starting) / starting) * 100
        to_win = summary["distance_to_win"]
        to_loss = summary["distance_to_loss"]

        lines = [
            header,
            "⚡ *Trading — Paper Challenge*",
            "",
            f"Equity: ${equity:.2f}",
            f"Cash: ${cash:.2f}",
            f"Starting Balance: ${starting:.2f}",
            f"Realized {self._term('P&L')}: ${summary['realized_pnl']:.2f}",
            f"Unrealized {self._term('P&L')}: ${summary['unrealized_pnl']:.2f}",
            f"Total Return: {total_return_pct:+.2f}%",
            f"Distance to +12% target: ${to_win:.2f}",
            f"Distance to -5% boundary: ${to_loss:.2f}",
            "",
        ]

        if summary.get("open_positions"):
            lines.append("*Open Positions:*")
            for pos in summary["open_positions"]:
                lines.append(
                    f"  • {pos['symbol']}: {pos['quantity']:.6f} @ ${pos['entry_price']:.2f}"
                )
        else:
            lines.append("Open Positions: None")

        lines.append("")

        if pending_signals:
            lines.append("*Pending Signals:*")
            for sig in pending_signals:
                exp = sig.get("expires_at", "Unavailable")
                if isinstance(exp, datetime):
                    exp = exp.strftime("%H:%M UTC")
                lines.append(f"  • {sig.get('asset', 'Unknown')}: "
                             f"{_esc(sig.get('type', 'Unknown'))} (expires {exp})")
        else:
            lines.append("Pending Signals: None")

        lines.append("")

        if last_signals:
            lines.append("*Market Regimes:*")
            for symbol, sig in last_signals.items():
                regime = sig.regime.value if hasattr(sig.regime, "value") else str(sig.regime)
                provider = getattr(sig, "provider", "Unavailable")
                lines.append(f"  • {symbol}: {_esc(regime)} (via {_esc(str(provider))})")
        else:
            lines.append("Market Regimes: Unavailable")

        lines.append("")

        paused = settings.agent_mode.value == "PAUSED"
        lines.append(f"Trading: {'Paused' if paused else 'Active'}")
        lines.append(f"System Health: {health_status}")

        if scheduler_info:
            last_check = scheduler_info.get("last_market_check", "Unavailable")
            next_check = scheduler_info.get("next_market_check", "Unavailable")
            lines.append(f"Last Market Check: {last_check}")
            lines.append(f"Next Market Check: {next_check}")
            fail_count = scheduler_info.get("failure_count")
            if fail_count:
                success_count = scheduler_info.get("success_count", 0)
                lines.append(f"Checks: {success_count} OK / {fail_count} failed")
                last_err = scheduler_info.get("last_error")
                if last_err:
                    lines.append(f"Last error: {_esc(last_err)}")

        if report_type == "evening":
            lines.append("")
            lines.append("_Entering night mode. Only emergency alerts until 08:00._")

        return "\n".join(lines)

    def _term(self, term: str) -> str:
        if self.beginner and term in BEGINNER_TERMS:
            return f"{term} ({BEGINNER_TERMS[term]})"
        return term
