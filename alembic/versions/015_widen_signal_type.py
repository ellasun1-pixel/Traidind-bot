"""Widen signal_type column to accommodate longer signal type names.

Revision ID: 015
Revises: 014
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("signals") as batch_op:
        batch_op.alter_column(
            "signal_type",
            type_=sa.String(30),
            existing_type=sa.String(20),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("signals") as batch_op:
        batch_op.alter_column(
            "signal_type",
            type_=sa.String(20),
            existing_type=sa.String(30),
            existing_nullable=False,
        )
