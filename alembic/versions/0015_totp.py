"""TOTP për çelësat e stafit

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sms_api_keys", sa.Column("totp_secret_enc", sa.Text(), nullable=True))
    op.add_column(
        "sms_api_keys",
        sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("sms_api_keys", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("sms_api_keys", "totp_last_step")
    op.drop_column("sms_api_keys", "totp_enabled")
    op.drop_column("sms_api_keys", "totp_secret_enc")
