"""Health observability - health_transitions table

Revision ID: 005
Revises: 004
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "health_transitions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("component", sa.String(30), nullable=False),
        sa.Column("old_status", sa.String(15), nullable=False),
        sa.Column("new_status", sa.String(15), nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("recovered_at", sa.DateTime(timezone=True)),
        sa.Column("recovery_seconds", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("health_transitions")
