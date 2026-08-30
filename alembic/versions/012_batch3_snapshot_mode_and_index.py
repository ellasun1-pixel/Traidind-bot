"""Batch 3: add agent_mode to portfolio_snapshots, fix unique index.

Revision ID: 012
Revises: 011
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "portfolio_snapshots",
        sa.Column("agent_mode", sa.String(20), nullable=True),
    )

    op.drop_index("uq_one_open_per_asset", table_name="paper_positions")
    op.execute(
        'CREATE UNIQUE INDEX uq_one_open_per_asset_mode '
        'ON paper_positions (asset_id, agent_mode) WHERE is_open = true'
    )


def downgrade() -> None:
    op.drop_index("uq_one_open_per_asset_mode", table_name="paper_positions")
    op.execute(
        "CREATE UNIQUE INDEX uq_one_open_per_asset "
        "ON paper_positions (asset_id) WHERE is_open = true"
    )
    op.drop_column("portfolio_snapshots", "agent_mode")
