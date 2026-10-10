"""Central: revision per entitet + numërues global transaksional + outbox (M7-b1).

Gjendja ekzistuese para M7 merr `revision = 1` dhe NUK krijon ngjarje historike (migrim ≠ replay):
snapshot-i i plotë i M7-c/d e mbulon. Pa ndryshim te Enterprise.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("enterprises", "enterprise_products"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False)
            )
            batch.create_check_constraint(f"ck_{table}_revision_positive", "revision >= 1")

    sequence = op.create_table(
        "sync_sequence",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_sync_sequence_singleton")),
        sa.CheckConstraint("last_seq >= 0", name=op.f("ck_sync_sequence_last_seq_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_sequence")),
    )
    op.bulk_insert(sequence, [{"id": 1, "last_seq": 0}])

    op.create_table(
        "sync_outbox",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1 and revision >= 1", name=op.f("ck_sync_outbox_positive")),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_sync_outbox_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_sync_outbox")),
        sa.UniqueConstraint("event_id", name=op.f("uq_sync_outbox_event_id")),
        sa.UniqueConstraint(
            "entity_type", "entity_id", "revision", name="uq_sync_outbox_entity_revision"
        ),
    )
    op.create_index("ix_sync_outbox_enterprise_id_seq", "sync_outbox", ["enterprise_id", "seq"])


def downgrade() -> None:
    op.drop_index("ix_sync_outbox_enterprise_id_seq", table_name="sync_outbox")
    op.drop_table("sync_outbox")
    op.drop_table("sync_sequence")
    for table in ("enterprise_products", "enterprises"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"ck_{table}_revision_positive", type_="check")
            batch.drop_column("revision")
