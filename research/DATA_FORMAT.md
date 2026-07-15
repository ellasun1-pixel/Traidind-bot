# Data Format

## CSV Format

Place files in `research/data/` with names like `BTC_USD.csv`.

Required columns:

| Column    | Type     | Description                          |
|-----------|----------|--------------------------------------|
| asset     | string   | Asset symbol, e.g. `BTC/USD`         |
| timestamp | datetime | UTC datetime, e.g. `2024-01-15T00:00:00+00:00` |
| open      | float    | Opening price                        |
| high      | float    | Highest price                        |
| low       | float    | Lowest price                         |
| close     | float    | Closing price                        |
| volume    | float    | Trading volume                       |
| source    | string   | Data source, e.g. `kraken`           |

Example:

```csv
asset,timestamp,open,high,low,close,volume,source
BTC/USD,2024-01-01T00:00:00+00:00,42500.0,43200.0,42100.0,42800.0,15000.5,kraken
BTC/USD,2024-01-02T00:00:00+00:00,42800.0,43500.0,42600.0,43100.0,12000.3,kraken
```

## Column Name Aliases

The importer recognizes these alternative column names:

- `timestamp`: `time`, `date`, `datetime`, `open_time`, `Date`, `Timestamp`
- `open`: `Open`, `o`
- `high`: `High`, `h`
- `low`: `Low`, `l`
- `close`: `Close`, `c`
- `volume`: `Volume`, `vol`, `v`
- `asset`: `symbol`, `pair`, `Asset`, `Symbol`

## JSON Format

Place files in `research/data/` with names like `BTC_USD.json`.

Array of objects with the same fields as CSV:

```json
[
  {
    "asset": "BTC/USD",
    "timestamp": "2024-01-01T00:00:00+00:00",
    "open": 42500.0,
    "high": 43200.0,
    "low": 42100.0,
    "close": 42800.0,
    "volume": 15000.5,
    "source": "kraken"
  }
]
```

## Raw Kraken JSON

If you save the raw response from `https://api.kraken.com/0/public/OHLC`, the importer will parse it automatically. Save it as a `.json` file in `research/data/`.

## Raw Coinbase JSON

If you save the raw response from Coinbase candles endpoint, the importer handles the `[time, low, high, open, close, volume]` format.

## Requirements

- Timestamps must be in UTC
- Candles should be daily (one per day)
- At least 252 candles are needed for indicator warm-up
- No duplicate timestamps
- OHLC must satisfy: `low ≤ open ≤ high` and `low ≤ close ≤ high`
- All prices must be positive
- Run `python -m research.validate_data` to check your files
