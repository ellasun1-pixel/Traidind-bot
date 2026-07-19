"""Portfolio snapshots - granular balance/equity tracking

Revision ID: 006
Revises: 005
Create Date: 2026-07-19
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("cash_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("equity_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("open_positions_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("open_positions_summary", sa.JSON),
        sa.Column("challenge_status", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("portfolio_snapshots")
