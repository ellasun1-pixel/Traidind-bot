# Running the Research Study on Render

Your Render deployment already has network access to Kraken and Coinbase. Here is how to run the full study there.

## Step 1: SSH into Render

Open a shell on your Render instance. You can do this from the Render dashboard under your service's "Shell" tab.

## Step 2: Navigate to the Project

```bash
cd /opt/render/project/src
```

(Or wherever your project is deployed.)

## Step 3: Fetch Data

```bash
python -m research.fetch_data --provider kraken --days 730
```

This downloads 2 years of daily candles for all 5 assets. Takes about 30 seconds.

To use Coinbase instead:

```bash
python -m research.fetch_data --provider coinbase --days 730
```

## Step 4: Validate Data

```bash
python -m research.validate_data
```

All files should show `[PASS]`. If any show `[FAIL]`, the error messages explain what to fix.

## Step 5: Run Backtest

```bash
python -m research.run_backtest --strategy conservative
```

To also test the challenge strategy:

```bash
python -m research.run_backtest --strategy challenge
```

## Step 6: Run Walk-Forward

```bash
python -m research.run_walk_forward --strategy conservative
```

## Step 7: Generate Report

```bash
python -m research.generate_report
```

The report is saved to `research/output/phase2_report.txt`.

## Step 8: Copy Results

Copy the output files back to your local machine:

```bash
# From your local machine:
scp render:/opt/render/project/src/research/output/* ./research/output/
```

Or just read the console output from steps 5-7.

## Safety Guarantees

This research framework:

- Does NOT start Telegram
- Does NOT start the scheduler
- Does NOT place any trades (paper or real)
- Does NOT modify the production database
- Does NOT read or use any API keys
- Only makes read-only GET requests to public Kraken/Coinbase endpoints
- Writes results only to `research/output/`

You can verify this by checking that `LIVE_TRADING_ENABLED` is not read by any research module:

```bash
grep -r "LIVE_TRADING" research/
```

This should return zero results.

## Full Pipeline (Copy-Paste)

```bash
cd /opt/render/project/src
python -m research.fetch_data --provider kraken --days 730
python -m research.validate_data
python -m research.run_backtest --strategy conservative
python -m research.run_backtest --strategy challenge
python -m research.run_walk_forward --strategy conservative
python -m research.run_walk_forward --strategy challenge
python -m research.generate_report
```

Total runtime: approximately 5-10 minutes depending on data size.
