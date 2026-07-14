from __future__ import annotations

import streamlit as st
import plotly.graph_objects as go

from src.scheduler.jobs import get_portfolio, get_last_signals
from src.config import settings


def main():
    st.set_page_config(page_title="Paper Challenge Agent", layout="wide")
    st.title("📊 Paper Challenge Agent")

    portfolio = get_portfolio()
    prices: dict[str, float] = {}
    summary = portfolio.get_portfolio_summary(prices)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Balance", f"${summary['balance_usd']:.2f}")
    with col2:
        st.metric("Equity", f"${summary['total_equity']:.2f}")
    with col3:
        st.metric("To Win", f"${summary['distance_to_win']:.2f}")
    with col4:
        status_emoji = {"active": "🟢", "won": "🏆", "lost": "🔴"}
        st.metric("Status", f"{status_emoji.get(summary['challenge_status'], '⚪')} {summary['challenge_status'].upper()}")

    st.subheader("Challenge Progress")
    balance = summary["balance_usd"]
    progress = (balance - settings.loss_level) / (settings.win_level - settings.loss_level)
    progress = max(0, min(1, progress))

    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=balance,
        delta={"reference": settings.starting_balance},
        gauge={
            "axis": {"range": [settings.loss_level, settings.win_level]},
            "bar": {"color": "green" if balance >= settings.starting_balance else "red"},
            "steps": [
                {"range": [settings.loss_level, 975], "color": "lightcoral"},
                {"range": [975, 1050], "color": "lightyellow"},
                {"range": [1050, 1090], "color": "lightgreen"},
                {"range": [1090, settings.win_level], "color": "limegreen"},
            ],
            "threshold": {
                "line": {"color": "blue", "width": 2},
                "thickness": 0.75,
                "value": settings.starting_balance,
            },
        },
        title={"text": "Balance ($)"},
    ))
    fig.update_layout(height=300)
    st.plotly_chart(fig, use_container_width=True)

    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("P&L")
        st.write(f"Realized: ${summary['realized_pnl']:.2f}")
        st.write(f"Unrealized: ${summary['unrealized_pnl']:.2f}")
        st.write(f"Drawdown: {summary['drawdown_pct']:.2f}%")
        st.write(f"Peak: ${summary['peak_balance']:.2f}")

    with col_right:
        st.subheader("Open Positions")
        if summary["open_positions"]:
            for pos in summary["open_positions"]:
                st.write(
                    f"**{pos['symbol']}**: {pos['quantity']:.6f} "
                    f"@ ${pos['entry_price']:.2f} "
                    f"(stop: ${pos['stop_loss']:.2f})"
                )
        else:
            st.write("_No open positions_")

    st.subheader("Latest Signals")
    last_signals = get_last_signals()
    if last_signals:
        for symbol, sig in last_signals.items():
            col_s1, col_s2, col_s3 = st.columns(3)
            with col_s1:
                st.write(f"**{symbol}**")
            with col_s2:
                st.write(f"Regime: {sig.regime.value}")
            with col_s3:
                color = {"BUY": "🟢", "SELL": "🔴", "NO_TRADE": "⚪"}.get(sig.signal_type, "🟡")
                st.write(f"{color} {sig.signal_type}")
    else:
        st.write("_No signals yet_")

    st.subheader("Trade History")
    if portfolio.closed_trades:
        for trade in portfolio.closed_trades[-20:]:
            pnl = trade.realized_pnl or 0
            emoji = "🟢" if pnl >= 0 else "🔴"
            st.write(
                f"{emoji} {trade.symbol}: {trade.quantity:.6f} @ ${trade.entry_price:.2f} "
                f"→ ${trade.exit_price:.2f} | P&L: ${pnl:.2f}"
            )
    else:
        st.write("_No trades yet_")

    st.subheader("Settings")
    st.write(f"Mode: {settings.agent_mode.value}")
    st.write(f"Beginner explanations: {settings.beginner_explanations}")
    st.write(f"Timezone: {settings.timezone}")
    st.write(f"Assets: {', '.join(a.symbol for a in settings.assets)}")


if __name__ == "__main__":
    main()
