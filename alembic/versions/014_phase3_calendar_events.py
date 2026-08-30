"""Phase 3: add calendar_events table for market event tracking.

Revision ID: 014
Revises: 013
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("unique_key", sa.String(120), nullable=False, unique=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("impact", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("currency", sa.String(10), server_default="USD"),
        sa.Column("asset_symbol", sa.String(20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("forecast", sa.String(50), nullable=True),
        sa.Column("previous", sa.String(50), nullable=True),
        sa.Column("alerted_24h", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("alerted_1h", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_calendar_event_time", "calendar_events", ["event_time"])


def downgrade() -> None:
    op.drop_index("ix_calendar_event_time", table_name="calendar_events")
    op.drop_table("calendar_events")
