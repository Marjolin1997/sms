"""M10-S0: kanonizimi i sender-ave — `norm_value`, historia append-only e vendimeve, provenienca e mesazhit. Aditiv.

- `sms_sender_ids`: `norm_value` (çelësi kanonik case-insensitive; backfill = lower(value)), `current_decision_id`, indeks kërkimi.
- `sms_sender_decisions`: historia VETËM-SHTIM (PostgreSQL: trigger-a që refuzojnë UPDATE/DELETE/TRUNCATE).
- Backfill: një rresht vendimi për çdo sender ekzistues (`source='backfill'`, aktori NULL: s'shpikim aktor) që pasqyron gjendjen aktuale.
- `sms_messages`: `sender_ref`, `sender_decision_ref`, `sender_policy_revision` (nullable; rreshtat historikë mbeten NULL).

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "sms_sender_ids",
        sa.Column("norm_value", sa.String(length=16), server_default="", nullable=False),
    )
    op.add_column("sms_sender_ids", sa.Column("current_decision_id", PK, nullable=True))
    op.execute("UPDATE sms_sender_ids SET norm_value = lower(value)")
    op.create_index(
        "ix_sms_sender_ids_lookup", "sms_sender_ids", ["owner_ref", "country", "norm_value"]
    )
    op.create_table(
        "sms_sender_decisions",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("sender_id", PK, nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("evidence_ref", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=12), server_default="local", nullable=False),
        sa.CheckConstraint(
            "decision in ('requested', 'approved', 'rejected', 'revoked', 'resubmitted')",
            name=op.f("ck_sms_sender_decisions_decision"),
        ),
        sa.CheckConstraint(
            "source in ('local', 'backfill')", name=op.f("ck_sms_sender_decisions_source")
        ),
        sa.CheckConstraint(
            "source = 'backfill' OR (decided_by IS NOT NULL AND length(trim(decided_by)) > 0)",
            name=op.f("ck_sms_sender_decisions_actor"),
        ),
        sa.CheckConstraint(
            "source = 'backfill' OR decision NOT IN ('rejected', 'revoked') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name=op.f("ck_sms_sender_decisions_reason_required"),
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["sms_sender_ids.id"],
            name=op.f("fk_sms_sender_decisions_sender_id_sms_sender_ids"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_decisions")),
    )
    op.create_index(
        op.f("ix_sms_sender_decisions_sender_id"),
        "sms_sender_decisions",
        ["sender_id"],
        unique=False,
    )
    op.create_index(
        "ix_sms_sender_decisions_sender_id_id",
        "sms_sender_decisions",
        ["sender_id", "id"],
        unique=False,
    )
    for col in ("sender_ref", "sender_decision_ref"):
        op.add_column("sms_messages", sa.Column(col, PK, nullable=True))
    op.add_column("sms_messages", sa.Column("sender_policy_revision", sa.Integer(), nullable=True))
    _backfill()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_decisions_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'sender decision history is append-only' USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_decisions_immutable BEFORE UPDATE OR DELETE ON sms_sender_decisions FOR EACH ROW EXECUTE FUNCTION sms_sender_decisions_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_decisions_no_truncate BEFORE TRUNCATE ON sms_sender_decisions FOR EACH STATEMENT EXECUTE FUNCTION sms_sender_decisions_forbid()"
        )


def _backfill() -> None:
    """Një vendim për sender ekzistues = gjendja e tij aktuale (burim `backfill`, aktor NULL). Aktori historik s'dihet kurrë me siguri, ndaj nuk shpikim."""
    bind = op.get_bind()
    senders = sa.table(
        "sms_sender_ids",
        sa.column("id"), sa.column("status"), sa.column("reviewed_at"),
        sa.column("reason"), sa.column("created_at"), sa.column("current_decision_id"),
    )  # fmt: skip
    dec = sa.table(
        "sms_sender_decisions",
        sa.column("id"), sa.column("sender_id"), sa.column("decision"), sa.column("from_status"),
        sa.column("to_status"), sa.column("decided_at"), sa.column("decided_by"), sa.column("reason"),
        sa.column("policy_revision"), sa.column("evidence_ref"), sa.column("source"),
    )  # fmt: skip
    kind = {
        "pending": "requested",
        "approved": "approved",
        "rejected": "rejected",
        "revoked": "revoked",
    }
    rows = bind.execute(
        sa.select(
            senders.c.id,
            senders.c.status,
            senders.c.reviewed_at,
            senders.c.reason,
            senders.c.created_at,
        ).order_by(senders.c.id)
    ).all()
    for sid, status, reviewed_at, reason, created_at in rows:
        status = status.lower() if isinstance(status, str) else str(status)
        did = bind.execute(
            sa.insert(dec)
            .values(
                sender_id=sid, decision=kind.get(status, "requested"), from_status=None,
                to_status=status, decided_at=reviewed_at or created_at, decided_by=None,
                reason=reason, policy_revision=None, evidence_ref=None, source="backfill",
            )
            .returning(dec.c.id)
        ).scalar()  # fmt: skip
        bind.execute(sa.update(senders).where(senders.c.id == sid).values(current_decision_id=did))


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_sms_sender_decisions_no_truncate ON sms_sender_decisions"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_sms_sender_decisions_immutable ON sms_sender_decisions"
        )
        op.execute("DROP FUNCTION IF EXISTS sms_sender_decisions_forbid()")
    op.drop_column("sms_messages", "sender_policy_revision")
    op.drop_column("sms_messages", "sender_decision_ref")
    op.drop_column("sms_messages", "sender_ref")
    op.drop_index("ix_sms_sender_decisions_sender_id_id", table_name="sms_sender_decisions")
    op.drop_index(op.f("ix_sms_sender_decisions_sender_id"), table_name="sms_sender_decisions")
    op.drop_table("sms_sender_decisions")
    op.drop_index("ix_sms_sender_ids_lookup", table_name="sms_sender_ids")
    op.drop_column("sms_sender_ids", "current_decision_id")
    op.drop_column("sms_sender_ids", "norm_value")
