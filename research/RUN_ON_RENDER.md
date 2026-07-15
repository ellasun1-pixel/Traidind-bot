# Running the Research Study on Render

Your Render deployment has network access to Kraken and Coinbase. Here is how to run the study.

## One Command (recommended)

```bash
cd /opt/render/project/src
python -m research.run_full_study --provider kraken --days 730
```

This runs the entire pipeline:
1. Downloads 2 years of daily candles for all 5 assets (~30 seconds)
2. Validates data integrity
3. Runs conservative strategy backtest on each asset
4. Runs walk-forward evaluation (train/validation/test splits)
5. Runs challenge simulation (1000 block-bootstrap runs)
6. Generates the final report

Total runtime: approximately 5-10 minutes.

The report is saved to `research/output/phase2_report.txt`.

## Step-by-Step (alternative)

```bash
cd /opt/render/project/src
python -m research.fetch_data --provider kraken --days 730
python -m research.validate_data
python -m research.run_backtest --strategy conservative
python -m research.run_walk_forward --strategy conservative
python -m research.generate_report
```

## Safety Guarantees

This research framework:

- Does NOT start the bot or scheduler
- Does NOT place any trades (paper or real)
- Does NOT modify the production database
- Does NOT read or use any API keys
- Only makes read-only GET requests to public Kraken/Coinbase endpoints
- Writes results only to `research/output/`

Verify with:

```bash
grep -r "LIVE_TRADING" research/
```

This should return zero results.

## Expected Output

After `run_full_study` completes, you will see:

```
  STUDY COMPLETE
  Elapsed: ~300 seconds
  Assets: BTC/USD, ETH/USD, XRP/USD, LINK/USD, LTC/USD
  Results: research/output
  Report:  research/output/phase2_report.txt
```

The `research/output/` directory will contain:
- `BTC_USD_conservative_backtest.json` (and one per asset)
- `BTC_USD_conservative_walkforward.json` (and one per asset)
- `phase2_report.txt`
