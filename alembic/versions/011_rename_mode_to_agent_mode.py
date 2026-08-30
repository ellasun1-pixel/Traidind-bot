"""Rename mode column to agent_mode.

PostgreSQL has a built-in aggregate function mode() WITHIN GROUP (...).
Using 'mode' as a column name causes WrongObjectType errors in queries.
Rename to 'agent_mode' to avoid the conflict.

Uses raw SQL with explicit column existence checks to handle all cases:
- Column is still named 'mode' -> rename it
- Column already named 'agent_mode' (re-run) -> skip
- Neither exists -> add agent_mode from scratch

Revision ID: 011
Revises: 010
Create Date: 2026-08-29
"""

import sys
from alembic import op
from sqlalchemy import text, inspect as sa_inspect

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def _get_columns(conn, table):
    insp = sa_inspect(conn)
    return [c["name"] for c in insp.get_columns(table)]


def upgrade():
    conn = op.get_bind()

    for table in ("paper_account", "paper_positions"):
        cols = _get_columns(conn, table)
        has_mode = "mode" in cols
        has_agent_mode = "agent_mode" in cols

        msg = f"011 migration: {table} — mode={has_mode}, agent_mode={has_agent_mode}"
        print(msg, flush=True)
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()

        if has_agent_mode:
            print(f"  -> {table}.agent_mode already exists, skipping", flush=True)
        elif has_mode:
            dialect = conn.dialect.name
            if dialect == "sqlite":
                conn.execute(text(
                    f'ALTER TABLE {table} RENAME COLUMN "mode" TO agent_mode'
                ))
            else:
                conn.execute(text(
                    f'ALTER TABLE {table} RENAME COLUMN "mode" TO agent_mode'
                ))
            print(f"  -> {table}: renamed mode -> agent_mode OK", flush=True)
        else:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN agent_mode VARCHAR(20) "
                f"NOT NULL DEFAULT 'PAPER_CHALLENGE'"
            ))
            print(f"  -> {table}: added agent_mode column OK", flush=True)

    print("011 migration: COMPLETE", flush=True)


def downgrade():
    conn = op.get_bind()
    for table in ("paper_account", "paper_positions"):
        cols = _get_columns(conn, table)
        if "agent_mode" in cols:
            conn.execute(text(
                f'ALTER TABLE {table} RENAME COLUMN agent_mode TO "mode"'
            ))
