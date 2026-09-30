"""inbox: SMS hyrës

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
    op.create_table(
        "sms_inbound_messages",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("public_id", sa.String(36), nullable=False, unique=True),
        sa.Column("owner_ref", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_message_id", sa.String(128)),
        sa.Column("to_number", sa.String(20), nullable=False),
        sa.Column("from_number", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("keyword_action", sa.String(16)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "provider_message_id", name="uq_sms_inbound_provider_msg"),
    )
    op.create_index(
        "ix_sms_inbound_thread", "sms_inbound_messages", ["owner_ref", "from_number", "id"]
    )
    op.create_index("ix_sms_inbound_received", "sms_inbound_messages", ["received_at"])


def downgrade() -> None:
    op.drop_table("sms_inbound_messages")
