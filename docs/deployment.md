# Deployment Guide — Render

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `APP_ENV` | Yes | Set to `production` |
| `DATABASE_URL` | Yes | Render PostgreSQL internal URL |
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `TELEGRAM_OWNER_IDS` | Yes | Owner's numeric Telegram user ID |
| `TELEGRAM_CHAT_ID` | Yes | Chat ID for proactive notifications (signals, reports). Get it from @userinfobot on Telegram. |
| `TELEGRAM_CHAT_IDS` | Yes | Comma-separated approved chat IDs for command authorization. Can be the same value as `TELEGRAM_CHAT_ID`. |
| `USER_TIMEZONE` | No | Default: `Asia/Jerusalem` |
| `LIVE_TRADING_ENABLED` | No | Must be `false` (default) |
| `PORT` | No | Render sets this automatically |

**Never set `LIVE_TRADING_ENABLED=true`.** The application will abort on startup if this is set.

## Build Command

```
pip install -r requirements.txt
```

## Start Command

```
alembic upgrade head && python main.py
```

## Health Checks

- **Liveness**: `GET /` returns `200 OK` immediately (process is alive)
- **Readiness**: `GET /health` returns `200` only after full initialization, `503` during startup

Configure Render health check to use `GET /health`.

## HTTP Server

The health server listens on `0.0.0.0:${PORT}` (Render provides the PORT variable).

## Startup Sequence

1. Environment validation
2. Health server starts
3. Database connection verified
4. Migrations applied (`alembic upgrade head`)
5. Authentication validated
6. Telegram bot created
7. Scheduler initialized (6 jobs)
8. Configuration logged
9. Startup sweep (clear stale locks, expire old signals)
10. Scheduler started
11. Readiness gate opens (`/health` returns 200)
12. Initial market check runs

## Database

- **Production**: PostgreSQL required (enforced by `APP_ENV=production`)
- **Development**: SQLite used automatically
- Migrations managed by Alembic (5 migrations as of Phase 1)

## Restart Behavior

- Stale scheduler locks are cleared on startup
- Expired signals are cleaned up
- Jobs with `misfire_grace_time` fire immediately if missed within the grace window
- Health transition history persists across restarts
