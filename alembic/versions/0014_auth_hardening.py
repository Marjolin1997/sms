"""allowlist IP për çelësa API dhe regjistër provash të dështuara

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sms_api_keys", sa.Column("allowed_cidrs", sa.Text(), nullable=True))
    op.create_table(
        "sms_auth_failures",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("ip", sa.String(45), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sms_auth_failures_ip", "sms_auth_failures", ["ip", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_sms_auth_failures_ip", table_name="sms_auth_failures")
    op.drop_table("sms_auth_failures")
    op.drop_column("sms_api_keys", "allowed_cidrs")
