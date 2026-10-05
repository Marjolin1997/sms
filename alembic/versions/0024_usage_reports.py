"""M9-d: outbox i raporteve të përdorimit financiar (Enterprise). Aditiv.

`sms_usage_reports`: raport kumulativ i ngrirë + gjendja e dërgimit (pending/sending/sent/retry/failed/superseded).
PostgreSQL: triggers që e bëjnë përmbajtjen e pandryshueshme dhe refuzojnë DELETE.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_STATUSES = ("pending", "sending", "sent", "retry", "failed", "superseded")
_FROZEN = ("report_id", "enterprise_id", "product_id", "currency", "report_seq", "authority_mode",
           "ledger_max_id", "generated_at", "payload_hash", "content_hash", "created_at")  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "sms_usage_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("report_seq", sa.BigInteger(), nullable=False),
        sa.Column("authority_mode", sa.String(length=8), nullable=False),
        sa.Column("ledger_max_id", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_sms_usage_reports")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "currency", "report_seq", name="uq_sms_usage_reports_seq"
        ),
        sa.CheckConstraint("report_seq > 0", name=op.f("ck_sms_usage_reports_seq_positive")),
        sa.CheckConstraint(
            "status in ('" + "', '".join(_STATUSES) + "')", name=op.f("ck_sms_usage_reports_status")
        ),
        sa.CheckConstraint(
            "authority_mode in ('local', 'shadow', 'central')",
            name=op.f("ck_sms_usage_reports_authority_mode"),
        ),
    )
    op.create_index(
        "ix_sms_usage_reports_status_next", "sms_usage_reports", ["status", "next_attempt_at"]
    )
    if op.get_bind().dialect.name == "postgresql":
        cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _FROZEN)
        cond += " OR NEW.payload::text IS DISTINCT FROM OLD.payload::text"
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_usage_report_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION 'usage report content is immutable' "
            "USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_usage_reports_guard BEFORE UPDATE ON sms_usage_reports "
            "FOR EACH ROW EXECUTE FUNCTION sms_usage_report_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_usage_reports_no_delete BEFORE DELETE ON sms_usage_reports "
            "FOR EACH ROW EXECUTE FUNCTION sms_money_forbid_delete()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_sms_usage_reports_no_delete ON sms_usage_reports")
        op.execute("DROP TRIGGER IF EXISTS trg_sms_usage_reports_guard ON sms_usage_reports")
        op.execute("DROP FUNCTION IF EXISTS sms_usage_report_guard()")
    op.drop_index("ix_sms_usage_reports_status_next", table_name="sms_usage_reports")
    op.drop_table("sms_usage_reports")
