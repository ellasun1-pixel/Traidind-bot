"""Add valid_candle_count and validation_error to market_data_meta

Revision ID: 003
Revises: 002
Create Date: 2026-07-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("market_data_meta") as batch_op:
        batch_op.add_column(sa.Column("valid_candle_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("validation_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("market_data_meta") as batch_op:
        batch_op.drop_column("validation_error")
        batch_op.drop_column("valid_candle_count")
