"""Expand scheduler_state for job locking and execution tracking

Revision ID: 004
Revises: 003
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduler_state") as batch_op:
        batch_op.add_column(sa.Column("lock_owner", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("current_status", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("success_count", sa.Integer(), nullable=True, server_default="0"))
        batch_op.add_column(sa.Column("failure_count", sa.Integer(), nullable=True, server_default="0"))
        batch_op.add_column(sa.Column("last_duration_ms", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scheduler_state") as batch_op:
        batch_op.drop_column("last_started_at")
        batch_op.drop_column("last_completed_at")
        batch_op.drop_column("last_duration_ms")
        batch_op.drop_column("failure_count")
        batch_op.drop_column("success_count")
        batch_op.drop_column("current_status")
        batch_op.drop_column("lock_expires_at")
        batch_op.drop_column("lock_owner")
