# Phase 2 — Historical Market Validation Framework

Research framework for evaluating the trading strategy on **real historical data** from Kraken and Coinbase.

## One-Command Study

Run the entire pipeline with a single command:

```bash
python -m research.run_full_study --provider kraken --days 730
```

This fetches data, validates it, runs the conservative backtest, walk-forward evaluation, challenge simulation, and generates the final report. No other steps required.

Options:
- `--provider kraken` or `--provider coinbase`
- `--days 730` (days of history, default 730)
- `--assets BTC/USD,ETH/USD` (specific assets, default all five)
- `--challenge-sims 1000` (simulation count, default 1000)
- `--skip-fetch` (use existing data in `research/data/`)

## Step-by-Step (alternative)

### 1. Fetch Data

```bash
python -m research.fetch_data --provider kraken --days 730
```

Downloads daily OHLCV for BTC/USD, ETH/USD, XRP/USD, LINK/USD, LTC/USD into `research/data/`.

### 2. Import from CSV/JSON (alternative to fetching)

Place files in `research/data/` with names like `BTC_USD.csv`. See [DATA_FORMAT.md](DATA_FORMAT.md) for format.

### 3. Validate Data

```bash
python -m research.validate_data
```

### 4. Run Backtest

```bash
python -m research.run_backtest --strategy conservative
```

### 5. Run Walk-Forward Evaluation

```bash
python -m research.run_walk_forward --strategy conservative
```

### 6. Generate Report

```bash
python -m research.generate_report
```

## What This Framework Does

1. **Data Ingestion** — Fetches from Kraken/Coinbase or imports CSV/JSON
2. **Data Validation** — Verifies timestamps, OHLC integrity, chronology
3. **Production Parity** — Uses the exact same `StrategyEngine` as the bot
4. **No Look-Ahead** — Signals from day T, execution at open of day T+1
5. **Walk-Forward** — Train/validation/test splits with proper warmup overlap
6. **Regime Analysis** — TREND/CHOP/LOWVOL/PANIC distribution per asset
7. **Signal Funnel** — Shows where candidates are filtered out
8. **Trade Diagnostics** — MFE/MAE, holding period, per-trade P&L
9. **Challenge Simulation** — Block bootstrap preserving market dependence
10. **Asset Universe Comparison** — BTC only vs pairs vs all five

## What This Framework Does NOT Do

- Start the bot or scheduler
- Place paper or real trades
- Modify production database tables
- Use synthetic GBM data
- Modify the production strategy

## File Structure

```
research/
  data/              ← OHLCV data files (CSV/JSON, gitignored)
  output/            ← Backtest results and reports (gitignored)
  schema.py          ← Canonical OHLCV schema
  fetch_data.py      ← Download from exchanges
  ingest.py          ← CSV/JSON import
  validate_data.py   ← Data validation
  backtest_engine.py ← Historical execution model
  walk_forward.py    ← Walk-forward splits
  regime_analysis.py ← Regime statistics
  metrics.py         ← Performance metrics
  challenge_sim.py   ← Challenge simulation
  run_backtest.py    ← CLI: run backtest
  run_walk_forward.py ← CLI: walk-forward
  generate_report.py ← CLI: report
  run_full_study.py  ← CLI: complete pipeline
```
