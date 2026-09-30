"""users, sessions, invite/reset tokens

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "sms_users",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(200)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("owner_ref", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "sms_user_sessions",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", PK, sa.ForeignKey("sms_users.id"), nullable=False),
        sa.Column("prefix", sa.String(12), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_agent", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_sms_user_sessions_user_id", "sms_user_sessions", ["user_id"])
    op.create_table(
        "sms_user_tokens",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("user_id", PK, sa.ForeignKey("sms_users.id"), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sms_user_tokens_user", "sms_user_tokens", ["user_id", "used_at"])


def downgrade() -> None:
    op.drop_table("sms_user_tokens")
    op.drop_table("sms_user_sessions")
    op.drop_table("sms_users")
