"""Add SOL, DOGE, AVAX, DOT assets

Revision ID: 007
Revises: 006
Create Date: 2026-07-19
"""
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        INSERT INTO assets (symbol, kraken_pair, coinbase_pair, risk_pct, max_position_usd, stop_loss_pct)
        VALUES
            ('SOL/USD', 'SOLUSD', 'SOL-USD', 0.003, 150.0, 0.03),
            ('DOGE/USD', 'XDGUSD', 'DOGE-USD', 0.003, 150.0, 0.03),
            ('AVAX/USD', 'AVAXUSD', 'AVAX-USD', 0.003, 150.0, 0.03),
            ('DOT/USD', 'DOTUSD', 'DOT-USD', 0.003, 150.0, 0.03)
        ON CONFLICT (symbol) DO NOTHING
    """)


def downgrade():
    op.execute("""
        DELETE FROM assets WHERE symbol IN ('SOL/USD', 'DOGE/USD', 'AVAX/USD', 'DOT/USD')
    """)
