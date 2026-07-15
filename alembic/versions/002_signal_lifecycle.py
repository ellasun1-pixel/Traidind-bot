"""Add signal lifecycle fields: confidence, market_snapshot, chaining, timestamps

Revision ID: 002
Revises: 001
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("signals") as batch_op:
        batch_op.add_column(sa.Column("confidence", sa.Numeric(5, 4), nullable=True))
        batch_op.add_column(sa.Column("market_snapshot", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("owner_decision_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("previous_signal_id", sa.String(36), nullable=True))
        batch_op.add_column(sa.Column("superseded_reason", sa.Text(), nullable=True))
        batch_op.alter_column("signal_type", type_=sa.String(20))
        batch_op.alter_column("priority", server_default="normal")
        batch_op.create_foreign_key(
            "fk_signals_previous_signal", "signals",
            ["previous_signal_id"], ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("signals") as batch_op:
        batch_op.drop_constraint("fk_signals_previous_signal", type_="foreignkey")
        batch_op.drop_column("superseded_reason")
        batch_op.drop_column("previous_signal_id")
        batch_op.drop_column("owner_decision_note")
        batch_op.drop_column("superseded_at")
        batch_op.drop_column("cancelled_at")
        batch_op.drop_column("market_snapshot")
        batch_op.drop_column("confidence")
