"""Central: feed-i `cp.sender.v1` — numërues global transaksional + outbox append-only (M10-S2). Aditiv.

`sender_sync_sequence` (singleton: epoch, last_seq, floor_seq) dhe `sender_sync_outbox` (gjendje e ngrirë per ngjarje; `enterprise_id` NULL = politikë globale; `group_id` lidh ngjarjet e të njëjtit
transaksion). PostgreSQL: outbox-i refuzon UPDATE/DELETE/TRUNCATE; numëruesi s'fshihet kurrë. Asnjë ndryshim i tabelave ekzistuese.

Revision ID: 0027
Revises: 0026
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_sync_sequence",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("floor_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_sender_sync_sequence_singleton")),
        sa.CheckConstraint(
            "last_seq >= 0", name=op.f("ck_sender_sync_sequence_last_seq_non_negative")
        ),
        sa.CheckConstraint(
            "floor_seq >= 0 and floor_seq <= last_seq",
            name=op.f("ck_sender_sync_sequence_floor_within_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_sync_sequence")),
    )
    seq = sa.table(
        "sender_sync_sequence",
        sa.column("id", sa.SmallInteger),
        sa.column("epoch", sa.Uuid),
        sa.column("last_seq", sa.BigInteger),
        sa.column("floor_seq", sa.BigInteger),
    )
    op.bulk_insert(seq, [{"id": 1, "epoch": uuid.uuid4(), "last_seq": 0, "floor_seq": 0}])
    op.create_table(
        "sender_sync_outbox",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "seq >= 1 and revision >= 1", name=op.f("ck_sender_sync_outbox_positive")
        ),
        sa.CheckConstraint(
            "event_type in ('sender.policy.upserted', 'sender.registry.upserted')",
            name=op.f("ck_sender_sync_outbox_event_type"),
        ),
        sa.CheckConstraint(
            "(event_type = 'sender.policy.upserted') = (enterprise_id IS NULL)",
            name=op.f("ck_sender_sync_outbox_policy_is_global"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_sender_sync_outbox_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_sender_sync_outbox")),
        sa.UniqueConstraint("event_id", name=op.f("uq_sender_sync_outbox_event_id")),
        sa.UniqueConstraint(
            "event_type", "entity_id", "revision", name="uq_sender_sync_outbox_entity_revision"
        ),
    )
    op.create_index(
        "ix_sender_sync_outbox_enterprise_seq", "sender_sync_outbox", ["enterprise_id", "seq"]
    )
    op.create_index("ix_sender_sync_outbox_group", "sender_sync_outbox", ["group_id", "seq"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION central_sender_sync_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_sync_outbox_immutable BEFORE UPDATE OR DELETE ON sender_sync_outbox FOR EACH ROW EXECUTE FUNCTION central_sender_sync_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_sync_outbox_no_truncate BEFORE TRUNCATE ON sender_sync_outbox FOR EACH STATEMENT EXECUTE FUNCTION central_sender_sync_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_sync_sequence_no_delete BEFORE DELETE ON sender_sync_sequence FOR EACH ROW EXECUTE FUNCTION central_sender_sync_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_sync_sequence_no_truncate BEFORE TRUNCATE ON sender_sync_sequence FOR EACH STATEMENT EXECUTE FUNCTION central_sender_sync_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sender_sync_sequence_no_truncate ON sender_sync_sequence",
            "DROP TRIGGER IF EXISTS trg_sender_sync_sequence_no_delete ON sender_sync_sequence",
            "DROP TRIGGER IF EXISTS trg_sender_sync_outbox_no_truncate ON sender_sync_outbox",
            "DROP TRIGGER IF EXISTS trg_sender_sync_outbox_immutable ON sender_sync_outbox",
            "DROP FUNCTION IF EXISTS central_sender_sync_forbid()",
        ):
            op.execute(stmt)
    op.drop_index("ix_sender_sync_outbox_group", table_name="sender_sync_outbox")
    op.drop_index("ix_sender_sync_outbox_enterprise_seq", table_name="sender_sync_outbox")
    op.drop_table("sender_sync_outbox")
    op.drop_table("sender_sync_sequence")
