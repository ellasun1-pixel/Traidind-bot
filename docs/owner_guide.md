# Owner Guide — Paper Challenge Agent

## What the Bot Does

This bot monitors five cryptocurrency markets and sends you simulated (paper) trading signals for the Kraken Funded Challenge. The goal is to grow a simulated $1,000 balance to $1,120 (+12%) without dropping below $950 (-5%).

**All trading is simulated.** No real orders are placed. No real money is at risk. No Kraken API trading key is used. No withdrawals are possible.

## What the Bot Does NOT Do

- Place real trades on any exchange
- Access your Kraken account funds
- Execute orders without your explicit confirmation
- Handle real money, withdrawals, or deposits
- Manage long-term investments or multiple portfolios

## Monitored Assets

The bot watches five assets on daily candles:

| Asset | Kraken Pair | Coinbase Pair |
|-------|-------------|---------------|
| BTC/USD | XXBTZUSD | BTC-USD |
| ETH/USD | XETHZUSD | ETH-USD |
| XRP/USD | XXRPZUSD | XRP-USD |
| LINK/USD | LINKUSD | LINK-USD |
| LTC/USD | XLTCZUSD | LTC-USD |

Market data is checked every **15 minutes** during active hours (08:00-23:00 Asia/Jerusalem).

## Signal Types

- **BUY** — The bot sees a favorable entry opportunity. Review the suggested price, stop-loss, and position size before confirming.
- **SELL** — The bot recommends exiting a position (stop-loss hit, market crash, or balance protection).
- **TAKE_PROFIT** — Profit target reached. Consider locking in gains.
- **NO_TRADE** — No actionable opportunity right now. The bot explains why.
- **WAIT** — Conditions are close but not yet confirmed. Keep watching.

## How Confirmation Works

1. The bot sends you a signal with full details.
2. You review the signal — price, risk, reasoning.
3. Reply `/confirm` to simulate the trade, or `/reject` to skip it.
4. **Without your confirmation, no simulated trade is placed. Ever.**

Signals expire after **30 minutes**. Expired signals cannot be confirmed.

## Telegram Commands

| Command | What it does |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | List all commands |
| `/status` | Current balance, regime, active signals |
| `/portfolio` | Full portfolio with equity, P&L, drawdown |
| `/signal` | Latest signal for each asset |
| `/history` | Recent simulated trade history |
| `/confirm` | Confirm pending signal(s) |
| `/reject` | Reject pending signal(s) |
| `/pause` | Pause signal generation |
| `/resume` | Resume signal generation |
| `/settings` | View or toggle settings |
| `/auth` | Authentication diagnostics |
| `/scheduler` | Job execution status |
| `/health` | Operational health dashboard |

All commands are restricted to the bot owner only.

## Pausing and Resuming

- `/pause` stops new signal generation. Market observation continues in the background.
- `/resume` restarts signal generation in Paper Challenge mode.

## Checking Health

Send `/health` to see the operational dashboard:
- Database connection status
- Scheduler job health
- Telegram connectivity
- Market data freshness
- Provider availability (Kraken primary, Coinbase fallback)
- Paper trading account status

## Why No Kraken API Key Is Required

The bot only reads public market data (OHLCV candles and prices) from Kraken and Coinbase public APIs. No authenticated endpoints are used. When you're ready for Phase 2 (live paper trading via Kraken API), an API key with read-only permissions will be added — but never with withdrawal permissions.

## Why No Real Money Can Be Traded

- `LIVE_TRADING_ENABLED` is hardcoded to `false`
- The startup sequence aborts if live trading is enabled
- No order submission code exists in the codebase
- All balances, positions, and P&L are simulated in a local database
- The system is designed for the Kraken Funded Challenge simulation only
