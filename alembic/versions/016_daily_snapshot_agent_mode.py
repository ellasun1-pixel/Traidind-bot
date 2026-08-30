"""Add agent_mode to daily_snapshots for mode isolation.

Revision ID: 016
Revises: 015
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table(
        "daily_snapshots",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch_op:
        batch_op.add_column(
            sa.Column("agent_mode", sa.String(20), nullable=False,
                       server_default="PAPER_CHALLENGE"),
        )
        batch_op.drop_constraint(
            "uq_daily_snapshots_snapshot_date", type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_daily_snapshot_date_mode",
            ["snapshot_date", "agent_mode"],
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_snapshots") as batch_op:
        batch_op.drop_constraint("uq_daily_snapshot_date_mode", type_="unique")
        batch_op.create_unique_constraint(
            None,
            ["snapshot_date"],
        )
        batch_op.drop_column("agent_mode")
