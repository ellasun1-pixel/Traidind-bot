"""Initial schema with all Phase 1 tables

Revision ID: 001
Revises:
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("kraken_pair", sa.String(20), nullable=True),
        sa.Column("coinbase_pair", sa.String(20), nullable=True),
        sa.Column("risk_pct", sa.Numeric(6, 4), nullable=False, server_default="0.003"),
        sa.Column("max_position_usd", sa.Numeric(12, 2), nullable=False, server_default="150.0"),
        sa.Column("stop_loss_pct", sa.Numeric(6, 4), nullable=False, server_default="0.03"),
        sa.Column("min_volume", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 8), nullable=False),
        sa.Column("high", sa.Numeric(18, 8), nullable=False),
        sa.Column("low", sa.Numeric(18, 8), nullable=False),
        sa.Column("close", sa.Numeric(18, 8), nullable=False),
        sa.Column("volume", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "timeframe", "open_time", name="uq_price_candle"),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("strategy_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("regime", sa.String(20), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("stop_loss", sa.Numeric(18, 8), nullable=True),
        sa.Column("take_profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("position_size_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("max_loss_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("order_type", sa.String(10), nullable=True),
        sa.Column("cancel_level", sa.Numeric(18, 8), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("price_range_low", sa.Numeric(18, 8), nullable=True),
        sa.Column("price_range_high", sa.Numeric(18, 8), nullable=True),
        sa.Column("price_tolerance_pct", sa.Numeric(6, 4), nullable=True, server_default="0.02"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "paper_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("balance_usd", sa.Numeric(12, 2), nullable=False, server_default="1000.00"),
        sa.Column("peak_balance", sa.Numeric(12, 2), nullable=False, server_default="1000.00"),
        sa.Column("starting_balance", sa.Numeric(12, 2), nullable=False, server_default="1000.00"),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
        sa.Column("daily_loss", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
        sa.Column("daily_loss_date", sa.Date(), nullable=False, server_default=sa.text("CURRENT_DATE")),
        sa.Column("challenge_status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("strategy_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("signal_id", sa.String(36), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(18, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=True),
        sa.Column("close_reason", sa.String(20), nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "trade_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("position_id", sa.Integer(), sa.ForeignKey("paper_positions.id"), nullable=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("signal_id", sa.String(36), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Numeric(18, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(18, 8), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_reason", sa.String(20), nullable=False),
        sa.Column("strategy_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "alert_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_type", sa.String(30), nullable=False),
        sa.Column("asset_symbol", sa.String(20), nullable=True),
        sa.Column("signal_id", sa.String(36), sa.ForeignKey("signals.id"), nullable=True),
        sa.Column("message_hash", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("actor", sa.String(50), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "scheduler_state",
        sa.Column("job_name", sa.String(50), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("job_name"),
    )

    op.create_table(
        "market_data_meta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("candle_count", sa.Integer(), nullable=False),
        sa.Column("oldest_candle", sa.DateTime(timezone=True), nullable=False),
        sa.Column("newest_candle", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("is_sufficient", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "timeframe", "source", name="uq_market_data_meta"),
    )

    op.create_table(
        "daily_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("balance_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("open_positions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("challenge_status", sa.String(10), nullable=False),
        sa.Column("peak_balance", sa.Numeric(12, 2), nullable=False),
        sa.Column("strategy_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_date"),
    )

    # Seed the 5 initial assets
    op.execute("""
        INSERT INTO assets (symbol, kraken_pair, coinbase_pair, risk_pct, max_position_usd, stop_loss_pct)
        VALUES
            ('BTC/USD', 'XXBTZUSD', 'BTC-USD', 0.003, 150.0, 0.03),
            ('ETH/USD', 'XETHZUSD', 'ETH-USD', 0.003, 150.0, 0.03),
            ('XRP/USD', 'XXRPZUSD', 'XRP-USD', 0.003, 150.0, 0.03),
            ('LINK/USD', 'LINKUSD', 'LINK-USD', 0.003, 150.0, 0.03),
            ('LTC/USD', 'XLTCZUSD', 'LTC-USD', 0.003, 150.0, 0.03)
    """)

    # Create initial paper account with $1000 starting balance
    op.execute("""
        INSERT INTO paper_account (balance_usd, peak_balance, starting_balance, strategy_version)
        VALUES (1000.00, 1000.00, 1000.00, '1.0')
    """)


def downgrade() -> None:
    op.drop_table("daily_snapshots")
    op.drop_table("market_data_meta")
    op.drop_table("scheduler_state")
    op.drop_table("audit_log")
    op.drop_table("alert_history")
    op.drop_table("app_settings")
    op.drop_table("trade_history")
    op.drop_table("paper_positions")
    op.drop_table("paper_account")
    op.drop_table("signals")
    op.drop_table("price_history")
    op.drop_table("assets")
