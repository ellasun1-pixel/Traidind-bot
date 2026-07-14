# Paper Challenge Investment Agent

Telegram-based agent for the Kraken Funded Paper Challenge ($1000 → $1120, loss limit $950).

The agent monitors crypto markets, generates trading signals, and manages a virtual portfolio.
It **never places real trades** — the user executes manually and confirms via Telegram.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your TELEGRAM_BOT_TOKEN
python main.py
```

## Architecture

| Module | Description |
|---|---|
| `market_data` | Kraken (primary) + Coinbase (fallback) price fetcher |
| `strategy` | Regime detection (TREND/CHOP/PANIC/LOWVOL) + signal generation |
| `risk` | Position sizing, risk budget, circuit breakers |
| `portfolio` | Virtual account with P&L tracking |
| `notifier` | Signal formatting (15 fields) + active hours / night mode |
| `scheduler` | 15-min market checks, morning/evening reports |
| `telegram_bot` | Commands: /status /portfolio /signal /confirm /pause etc. |
| `web_panel` | Streamlit dashboard |

## Tests

```bash
python -m pytest tests/ -v
```

## Docs

- [Technical Specification](docs/TECHNICAL_SPECIFICATION_v1.0.md)
- [Strategy Specification](docs/Strategy_Specification_v1.0.md)
- [Beginner's Guide](docs/BEGINNER_GUIDE.md)
