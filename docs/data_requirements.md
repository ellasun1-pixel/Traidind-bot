# Data Requirements

## Market Data Sources

| Provider | Role | API | Authentication |
|----------|------|-----|----------------|
| Kraken | Primary | Public REST (OHLC) | None required |
| Coinbase | Fallback | Public REST (candles) | None required |

Both providers use only public endpoints. No API keys are needed for Phase 1.

## Candle Requirements

- **Timeframe**: Daily (1d)
- **Minimum valid candles**: 250 (for 200-day EMA calculation)
- **Target fetch count**: 300
- **Maximum candle age**: 30 hours (daily candle freshness)

## Validation Checks (13 total)

Each candle is validated for:
1. Non-null open, high, low, close, volume
2. High >= Low
3. High >= Open and High >= Close
4. Low <= Open and Low <= Close
5. All prices > 0
6. Volume >= 0
7. Open time is a valid datetime
8. No duplicate timestamps
9. Chronological ordering
10. No gaps exceeding 2x expected interval
11. Price changes within reasonable bounds (no >50% single-candle moves)
12. Sufficient candle count (>= min_valid_candles)
13. Candle freshness (newest candle < max_daily_candle_age_hours old)

## Persistence Rules

- **Valid datasets**: Candles stored in `price_history`, metadata in `market_data_meta`
- **Invalid datasets**: Only metadata stored (for diagnostics), no `price_history` rows created
- Invalid data never overwrites previously valid data

## Price Divergence

Provider prices are compared before analysis. If Kraken and Coinbase prices diverge by more than 5% (configurable), analysis is blocked to prevent acting on stale or manipulated data.
