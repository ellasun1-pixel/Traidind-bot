# Technical Specification v1.0
### Paper Challenge Investment Agent — Kraken Funded Challenge

---

## 1. Architecture Overview

The system is a modular Python application that monitors cryptocurrency markets,
generates trading signals based on the Strategy Specification v1.0, and delivers
notifications to the user via Telegram. No real trades are executed — the agent
manages a virtual (paper) portfolio and tracks Challenge progress.

### Module Diagram

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│ Market Data │────▶│   Strategy   │────▶│   Notifier   │──▶ Telegram Bot
│   Layer     │     │   Engine     │     │              │
└─────────────┘     └──────┬───────┘     └──────────────┘
                           │
                    ┌──────▼───────┐
                    │    Risk      │
                    │   Manager   │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │    Paper     │
                    │  Portfolio   │
                    └──────────────┘

┌──────────────┐     ┌──────────────┐
│  Scheduler   │     │  Web Panel   │
│ (APScheduler)│     │ (Streamlit)  │
└──────────────┘     └──────────────┘
```

### Modules

| Module | Responsibility |
|---|---|
| `market_data` | Fetch prices from Kraken (primary) and Coinbase (fallback), cache, build candles, detect source divergence |
| `strategy` | Classify market regime (TREND/CHOP/PANIC/LOWVOL), compute indicators (EMA, ADX, ER), generate signals per Strategy Spec v1.0 |
| `risk` | Enforce hard limits (no leverage/shorts/futures), position sizing via risk formula, circuit breakers by balance level |
| `portfolio` | Virtual account: balance, positions, P&L (realized/unrealized), drawdown, challenge status tracking |
| `notifier` | Format signals (15 mandatory fields), apply notification logic (active hours, night mode, anti-spam) |
| `scheduler` | APScheduler-based: 15-min market checks, 08:00 morning report, 22:30 evening report (Asia/Jerusalem) |
| `telegram_bot` | Telegram interface: commands `/status /portfolio /signal /history /confirm /reject /pause /resume /settings /help` |
| `web_panel` | Streamlit dashboard: portfolio overview, signal history, P&L chart |

---

## 2. Database Schema (SQLite / PostgreSQL)

```sql
-- Tracked assets
CREATE TABLE assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL UNIQUE,       -- e.g. "BTC/USD"
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- OHLCV price history
CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    source TEXT NOT NULL,               -- "kraken" / "coinbase"
    timeframe TEXT NOT NULL,            -- "1d" / "4h" / "1h"
    open_time TIMESTAMP NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(asset_id, source, timeframe, open_time)
);

-- Generated signals
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    signal_type TEXT NOT NULL,          -- BUY/SELL/REDUCE/TAKE_PROFIT/MOVE_TO_USD/NO_TRADE
    priority TEXT NOT NULL,             -- CRITICAL/HIGH/MEDIUM
    regime TEXT NOT NULL,               -- TREND/CHOP/PANIC/LOWVOL
    entry_price REAL,
    stop_loss REAL,
    position_size_usd REAL,
    max_loss_usd REAL,
    reason TEXT,
    signal_data JSON,                   -- full 15-field signal as JSON
    status TEXT DEFAULT 'pending',      -- pending/confirmed/rejected/expired
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP,
    notified_at TIMESTAMP
);

-- Confirmed virtual trades
CREATE TABLE virtual_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    side TEXT NOT NULL,                 -- BUY/SELL
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    position_value_usd REAL NOT NULL,
    commission_usd REAL DEFAULT 0,
    spread_cost_usd REAL DEFAULT 0,
    exit_price REAL,
    exit_at TIMESTAMP,
    realized_pnl REAL,
    status TEXT DEFAULT 'open',         -- open/closed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Current portfolio snapshot
CREATE TABLE portfolio_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    balance_usd REAL NOT NULL,
    total_equity REAL NOT NULL,
    unrealized_pnl REAL DEFAULT 0,
    realized_pnl REAL DEFAULT 0,
    drawdown_pct REAL DEFAULT 0,
    peak_balance REAL NOT NULL,
    distance_to_win REAL,
    distance_to_loss REAL,
    challenge_status TEXT DEFAULT 'active', -- active/won/lost
    open_positions_count INTEGER DEFAULT 0,
    snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User settings
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Audit log
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    details JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 3. Data Sources

| Priority | Source | API | Use |
|---|---|---|---|
| Primary | Kraken | `https://api.kraken.com/0/public/OHLC` | OHLCV candles, ticker |
| Fallback | Coinbase | `https://api.exchange.coinbase.com/products/{id}/candles` | OHLCV candles |

### Divergence detection
Before emitting any signal, compare the latest close prices from both sources.
If they diverge by more than the configured threshold (default: 1.5%), the signal
is suppressed and a warning is logged.

---

## 4. Telegram Integration

### Commands
| Command | Description |
|---|---|
| `/status` | Current regime, balance, active signals |
| `/portfolio` | Full portfolio view: positions, P&L, drawdown |
| `/signal` | Last generated signal with all 15 fields |
| `/history` | Recent trade history |
| `/confirm <id>` | Confirm a pending signal → execute virtual trade |
| `/reject <id>` | Reject a pending signal |
| `/pause` | Pause signal generation |
| `/resume` | Resume signal generation |
| `/settings` | View/toggle settings (e.g. BEGINNER_EXPLANATIONS) |
| `/help` | Show available commands |

---

## 5. Cloud Deployment — Render

**Choice: Render** (over Railway) because:
- Free tier includes a persistent web service suitable for the Telegram webhook + Streamlit panel.
- Native support for environment variables (Telegram token).
- Simple Git-based deploys.
- Background workers supported for the scheduler.
- PostgreSQL addon available on free tier if needed.

### Deployment architecture on Render
- **Web Service**: Runs the Telegram bot (webhook mode) + Streamlit panel.
- **Background Worker** (or combined process): APScheduler for 15-min checks and scheduled reports.
- **Database**: SQLite file (sufficient for single-user paper trading) stored on persistent disk, or Render PostgreSQL if scaling is needed.
- **Environment Variables**: `TELEGRAM_BOT_TOKEN`, `AGENT_MODE`, `BEGINNER_EXPLANATIONS`.
