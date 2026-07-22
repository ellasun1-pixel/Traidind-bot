"""Add partial unique index: one open position per asset.

Revision ID: 008
Revises: 007
Create Date: 2026-07-22
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_per_asset "
        "ON paper_positions (asset_id) WHERE is_open = true"
    )


def downgrade() -> None:
    op.drop_index("uq_one_open_per_asset", table_name="paper_positions")
