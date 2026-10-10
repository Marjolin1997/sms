"""Enterprise: outbox-i i kërkesave të sender-ave drejt Central (`sender.request.v1`, M10-S3). Aditiv; `sms_sender_ids` dhe historia e vendimeve nuk preken.

`sms_sender_request_outbox`: një rresht për veprim logjik (kërkesë/ridërgim); përmbajtja e ngrirë (trigger PG), nuk fshihet kurrë; një `requested` për sender (indeks i pjesshëm UNIQUE).

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
FROZEN = (
    "operation_id", "enterprise_id", "sender_id", "external_ref", "request_type", "schema_version",
    "request_hash", "created_at",
)  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "sms_sender_request_outbox",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", PK, nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("request_type", sa.String(length=12), nullable=False),
        sa.Column("schema_version", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=32), nullable=True),
        sa.Column("ack_outcome", sa.String(length=24), nullable=True),
        sa.Column("ack_registry_ref", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state in ('pending', 'sending', 'sent', 'retry', 'failed')",
            name=op.f("ck_sms_sender_request_outbox_state"),
        ),
        sa.CheckConstraint(
            "request_type in ('requested', 'resubmitted')",
            name=op.f("ck_sms_sender_request_outbox_request_type"),
        ),
        sa.CheckConstraint(
            "external_ref = 'sms-sender-' || CAST(sender_id AS VARCHAR)",
            name=op.f("ck_sms_sender_request_outbox_external_ref"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_sms_sender_request_outbox_attempts")),
        sa.ForeignKeyConstraint(
            ["sender_id"],
            ["sms_sender_ids.id"],
            name=op.f("fk_sms_sender_request_outbox_sender_id_sms_sender_ids"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_request_outbox")),
        sa.UniqueConstraint("operation_id", name=op.f("uq_sms_sender_request_outbox_operation_id")),
    )
    op.create_index(
        op.f("ix_sms_sender_request_outbox_enterprise_id"),
        "sms_sender_request_outbox",
        ["enterprise_id"],
    )
    op.create_index(
        "ix_sms_sender_request_outbox_state_next",
        "sms_sender_request_outbox",
        ["state", "next_attempt_at"],
    )
    op.create_index(
        "ix_sms_sender_request_outbox_sender", "sms_sender_request_outbox", ["sender_id", "id"]
    )
    op.create_index(
        "uq_sms_sender_request_outbox_one_request",
        "sms_sender_request_outbox",
        ["sender_id"],
        unique=True,
        sqlite_where=sa.text("request_type = 'requested'"),
        postgresql_where=sa.text("request_type = 'requested'"),
    )
    if op.get_bind().dialect.name == "postgresql":
        cond = (
            " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in FROZEN)
            + " OR NEW.payload::text IS DISTINCT FROM OLD.payload::text"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_request_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION 'sender request content is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_request_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are never deleted', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_request_outbox_guard BEFORE UPDATE ON sms_sender_request_outbox FOR EACH ROW EXECUTE FUNCTION sms_sender_request_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_request_outbox_no_delete BEFORE DELETE ON sms_sender_request_outbox FOR EACH ROW EXECUTE FUNCTION sms_sender_request_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_request_outbox_no_truncate BEFORE TRUNCATE ON sms_sender_request_outbox FOR EACH STATEMENT EXECUTE FUNCTION sms_sender_request_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sms_sender_request_outbox_no_truncate ON sms_sender_request_outbox",
            "DROP TRIGGER IF EXISTS trg_sms_sender_request_outbox_no_delete ON sms_sender_request_outbox",
            "DROP TRIGGER IF EXISTS trg_sms_sender_request_outbox_guard ON sms_sender_request_outbox",
            "DROP FUNCTION IF EXISTS sms_sender_request_forbid()",
            "DROP FUNCTION IF EXISTS sms_sender_request_guard()",
        ):
            op.execute(stmt)
    op.drop_index(
        "uq_sms_sender_request_outbox_one_request", table_name="sms_sender_request_outbox"
    )
    op.drop_index("ix_sms_sender_request_outbox_sender", table_name="sms_sender_request_outbox")
    op.drop_index("ix_sms_sender_request_outbox_state_next", table_name="sms_sender_request_outbox")
    op.drop_index(
        op.f("ix_sms_sender_request_outbox_enterprise_id"), table_name="sms_sender_request_outbox"
    )
    op.drop_table("sms_sender_request_outbox")
