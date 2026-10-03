"""Campaigns me kanal email: channel, subject/html/from, email_id te marrësit.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

FK = "fk_sms_campaign_recipients_email_id_sms_emails"


def upgrade() -> None:
    # batch_alter_table: PostgreSQL bën ALTER të drejtpërdrejtë; SQLite (vetëm zhvillim) rindërton tabelën
    with op.batch_alter_table("sms_campaign_recipients") as b:
        b.add_column(sa.Column("email_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite")))
        b.alter_column(
            "address", existing_type=sa.String(16), type_=sa.String(254), existing_nullable=True
        )
        b.create_index("ix_sms_camp_recipients_email", ["email_id"])
        b.create_foreign_key(FK, "sms_emails", ["email_id"], ["id"])
    with op.batch_alter_table("sms_campaigns") as b:
        b.add_column(sa.Column("channel", sa.String(8), server_default="sms", nullable=False))
        b.add_column(sa.Column("subject", sa.String(200)))
        b.add_column(sa.Column("html_body", sa.Text()))
        b.add_column(sa.Column("from_email", sa.String(254)))
        b.add_column(sa.Column("from_name", sa.String(100)))
        b.alter_column(
            "text", existing_type=sa.String(1600), type_=sa.Text(), existing_nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("sms_campaigns") as b:
        b.alter_column(
            "text", existing_type=sa.Text(), type_=sa.String(1600), existing_nullable=True
        )
        for col in ("from_name", "from_email", "html_body", "subject", "channel"):
            b.drop_column(col)
    with op.batch_alter_table("sms_campaign_recipients") as b:
        b.drop_constraint(FK, type_="foreignkey")
        b.drop_index("ix_sms_camp_recipients_email")
        b.alter_column(
            "address", existing_type=sa.String(254), type_=sa.String(16), existing_nullable=True
        )
        b.drop_column("email_id")
