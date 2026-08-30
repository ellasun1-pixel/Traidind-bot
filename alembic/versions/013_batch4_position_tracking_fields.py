"""Batch 4: add peak_price, tp1_fired, tp2_fired to paper_positions.

These fields were previously only in memory and lost on restart,
causing trailing stops to reset and take-profit levels to re-fire.

Revision ID: 013
Revises: 012
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "paper_positions",
        sa.Column("peak_price", sa.Numeric(18, 8), nullable=True),
    )
    op.add_column(
        "paper_positions",
        sa.Column("tp1_fired", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "paper_positions",
        sa.Column("tp2_fired", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("paper_positions", "tp2_fired")
    op.drop_column("paper_positions", "tp1_fired")
    op.drop_column("paper_positions", "peak_price")
