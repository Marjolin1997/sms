"""SMS hyrës (inbox) dhe fjalë kyçe

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "sms_inbound_messages",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("owner_ref", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("from_number", sa.String(20), nullable=False),
        sa.Column("to_number", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("action", sa.String(16)),
        sa.Column("keyword", sa.String(32)),
        sa.Column("reply_status", sa.String(64)),
        sa.Column("reply_message_id", sa.String(36)),
        sa.Column("contact_id", PK),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_message_id", name="uq_sms_inbound_provider_msg"),
    )
    op.create_index("ix_sms_inbound_owner", "sms_inbound_messages", ["owner_ref", "id"])
    op.create_index("ix_sms_inbound_from", "sms_inbound_messages", ["owner_ref", "from_number"])
    op.create_table(
        "sms_keywords",
        sa.Column("id", PK, primary_key=True, autoincrement=True),
        sa.Column("owner_ref", sa.String(64), nullable=False),
        sa.Column("keyword", sa.String(32), nullable=False),
        sa.Column("reply_text", sa.String(480)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_ref", "keyword", name="uq_sms_keywords_owner_kw"),
    )


def downgrade() -> None:
    op.drop_table("sms_keywords")
    op.drop_index("ix_sms_inbound_from", table_name="sms_inbound_messages")
    op.drop_index("ix_sms_inbound_owner", table_name="sms_inbound_messages")
    op.drop_table("sms_inbound_messages")
