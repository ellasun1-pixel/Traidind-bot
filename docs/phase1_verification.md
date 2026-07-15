# Phase 1 Verification Checklist

## Safety Constraints

- [ ] `LIVE_TRADING_ENABLED=false` in all environments
- [ ] Startup aborts if `LIVE_TRADING_ENABLED=true`
- [ ] No order submission code exists
- [ ] No withdrawal functionality exists
- [ ] API keys never printed in logs or messages
- [ ] All balances and trades are simulated
- [ ] Paper trades never mixed with real trades
- [ ] No Kraken trading API key used
- [ ] Confirmation required before any simulated trade

## Telegram Commands (all owner-only)

- [ ] `/start` — welcome message
- [ ] `/help` — command list
- [ ] `/status` — balance, regime, signals
- [ ] `/portfolio` — full portfolio view
- [ ] `/signal` — latest signals (including NO_TRADE explanations)
- [ ] `/history` — trade history
- [ ] `/confirm` — confirm pending signals
- [ ] `/reject` — reject pending signals
- [ ] `/pause` — pause signal generation
- [ ] `/resume` — resume signal generation
- [ ] `/settings` — view/toggle settings
- [ ] `/auth` — authentication diagnostics
- [ ] `/scheduler` — job execution status
- [ ] `/health` — operational health dashboard

## Health Monitoring

- [ ] 7 components checked (database, scheduler, telegram, market_data, providers, signal_engine, paper_trading)
- [ ] Aggregation: DB/scheduler UNHEALTHY → system UNHEALTHY
- [ ] Health transitions recorded in database
- [ ] Recovery time tracked
- [ ] Owner notified on UNHEALTHY/recovery transitions
- [ ] Duplicate notifications suppressed
- [ ] Telegram API verified via getMe (cached 5 min)

## Market Data

- [ ] Kraken primary, Coinbase fallback
- [ ] 13 validation checks on every candle
- [ ] Only validated candles persisted to price_history
- [ ] Invalid datasets update metadata only
- [ ] Price divergence gate blocks analysis if >5%
- [ ] Data freshness enforced (<30h for daily candles)

## Scheduler

- [ ] 6 jobs: market_check, expire_signals, morning_report, evening_report, health_heartbeat, health_check
- [ ] Atomic locking prevents duplicate runs
- [ ] Stale locks cleared on startup
- [ ] Misfire grace time on all jobs
- [ ] Report idempotency (one report per day)

## Signals

- [ ] Immutable lifecycle: pending → confirmed/rejected/expired/superseded
- [ ] Equivalent signals suppressed (type + price 2% + stop_loss 2% + priority)
- [ ] Materially changed signals supersede old ones with previous_signal_id
- [ ] 30-minute expiry
- [ ] Expired signals cleaned up every 5 minutes

## Reports

- [ ] Morning report at 08:00 Asia/Jerusalem
- [ ] Evening report at 22:30 Asia/Jerusalem
- [ ] Reports include: balance, P&L, positions, signals, regime, health
- [ ] Paper Challenge label on every report
- [ ] Night mode note on evening report

## Production Deployment

- [ ] PostgreSQL enforced in production
- [ ] Health endpoint returns 503 during startup, 200 after
- [ ] Liveness endpoint always returns 200
- [ ] Startup diagnostics log 8 verification steps
- [ ] Alembic migrations run before application start
