"""Backfill mode column for existing rows.

Migration 009 added the mode column with server_default, but PostgreSQL 11+
stores virtual defaults in the catalog without physically writing to existing
rows. This can cause ORM filters like `WHERE mode = 'PAPER_CHALLENGE'` to miss
rows whose value is virtual. This migration explicitly writes the value to
every row, making it physical and queryable.

Revision ID: 010
Revises: 009
Create Date: 2026-08-29
"""

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE paper_account SET mode = 'PAPER_CHALLENGE' WHERE mode IS NULL OR mode = ''")
    op.execute("UPDATE paper_positions SET mode = 'PAPER_CHALLENGE' WHERE mode IS NULL OR mode = ''")
    op.execute("UPDATE paper_account SET mode = mode")
    op.execute("UPDATE paper_positions SET mode = mode")


def downgrade():
    pass
