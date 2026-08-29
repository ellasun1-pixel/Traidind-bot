"""Rename mode column to agent_mode.

PostgreSQL has a built-in aggregate function mode() WITHIN GROUP (...).
Using 'mode' as a column name causes WrongObjectType errors in queries.
Rename to 'agent_mode' to avoid the conflict.

Revision ID: 011
Revises: 010
Create Date: 2026-08-29
"""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("paper_account", "mode", new_column_name="agent_mode")
    op.alter_column("paper_positions", "mode", new_column_name="agent_mode")


def downgrade():
    op.alter_column("paper_account", "agent_mode", new_column_name="mode")
    op.alter_column("paper_positions", "agent_mode", new_column_name="mode")
