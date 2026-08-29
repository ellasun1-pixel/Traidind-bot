"""Add mode column to paper_account and paper_positions.

Revision ID: 009
Revises: 008
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "paper_account",
        sa.Column("mode", sa.String(20), nullable=False, server_default="PAPER_CHALLENGE"),
    )
    op.add_column(
        "paper_positions",
        sa.Column("mode", sa.String(20), nullable=False, server_default="PAPER_CHALLENGE"),
    )


def downgrade():
    op.drop_column("paper_positions", "mode")
    op.drop_column("paper_account", "mode")
