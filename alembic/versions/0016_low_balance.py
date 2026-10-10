"""prag njoftimi për bilancë të ulët

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sms_wallets", sa.Column("low_balance_threshold", sa.Numeric(20, 6), nullable=True)
    )
    op.add_column(
        "sms_wallets",
        sa.Column("low_balance_notified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("sms_wallets", "low_balance_notified")
    op.drop_column("sms_wallets", "low_balance_threshold")
