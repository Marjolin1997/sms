"""Enterprise: siguria e provider-it/DLR (M11-J). Aditiv: indekse + 3 kolona nullable/me default; pa rishkrim historie.

  · `uq_sms_messages_provider_msg`: UNIQUE (provider, provider_message_id). NULL-et mbeten të lejuara shumëfish. Nëse ekzistojnë
    dublikata historikë, migrimi NDALET me mesazh të qartë dhe NUK fshin/rishkruan asgjë: `python -m scripts.provider_readiness
    duplicate-ids` (vetëm-lexim) i liston; rakordohen manualisht, pastaj migrimi rinis.
  · `sms_dlr_receipts`: `fingerprint` (NULL për historikët), `seen_count` (default 1), `last_seen_at`; UNIQUE (fingerprint, outcome)
    (NULL-et të lejuara) dhe indeks mbi `received_at` për retention.

Revision ID: 0035
Revises: 0034
"""

import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dups = (
        op.get_bind()
        .execute(
            sa.text(
                "select count(*) from (select 1 from sms_messages where provider_message_id is not null"
                " group by provider, provider_message_id having count(*) > 1) d"
            )
        )
        .scalar()
    )
    if dups:
        raise RuntimeError(
            f"{dups} duplicate (provider, provider_message_id) group(s) exist in sms_messages: refusing to add the unique index. "
            "Nothing was changed. List them with `python -m scripts.provider_readiness duplicate-ids`, reconcile them manually "
            "(never delete history), then run the migration again."
        )
    op.create_index(
        "uq_sms_messages_provider_msg",
        "sms_messages",
        ["provider", "provider_message_id"],
        unique=True,
    )
    with op.batch_alter_table("sms_dlr_receipts") as b:
        b.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        b.add_column(sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"))
        b.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "uq_sms_dlr_receipts_fp", "sms_dlr_receipts", ["fingerprint", "outcome"], unique=True
    )
    op.create_index("ix_sms_dlr_receipts_received", "sms_dlr_receipts", ["received_at"])


def downgrade() -> None:
    op.drop_index("ix_sms_dlr_receipts_received", table_name="sms_dlr_receipts")
    op.drop_index("uq_sms_dlr_receipts_fp", table_name="sms_dlr_receipts")
    with op.batch_alter_table("sms_dlr_receipts") as b:
        b.drop_column("last_seen_at")
        b.drop_column("seen_count")
        b.drop_column("fingerprint")
    op.drop_index("uq_sms_messages_provider_msg", table_name="sms_messages")
