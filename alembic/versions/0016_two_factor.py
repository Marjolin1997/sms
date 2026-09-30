"""2FA (TOTP) dhe kodet e rikuperimit

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.add_column("sms_users", sa.Column("totp_secret_enc", sa.Text()))
    op.add_column("sms_users", sa.Column("totp_enabled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "sms_users",
        sa.Column("totp_last_step", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "sms_user_recovery_codes",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", PK, sa.ForeignKey("sms_users.id"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sms_user_recovery_codes_user_id", "sms_user_recovery_codes", ["user_id"])


def downgrade() -> None:
    op.drop_table("sms_user_recovery_codes")
    op.drop_column("sms_users", "totp_last_step")
    op.drop_column("sms_users", "totp_enabled_at")
    op.drop_column("sms_users", "totp_secret_enc")
