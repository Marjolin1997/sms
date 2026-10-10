"""Enterprise: prova e faturueshmërisë së email-it + outbox i raporteve kumulative (M9-g2). Aditiv.

`sms_email_billable_events` (UNIQUE email_id; append-only) dhe `sms_billing_usage_reports` (përmbajtja e ngrirë). PostgreSQL: trigger-a që
refuzojnë UPDATE/DELETE/TRUNCATE mbi provën dhe DELETE/ndryshim të përmbajtjes mbi raportet.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_billing_usage_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("report_seq", sa.BigInteger(), nullable=False),
        sa.Column("watermark", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_billable_count", sa.BigInteger(), nullable=False),
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
        sa.CheckConstraint(
            "status in ('pending', 'sending', 'sent', 'retry', 'failed', 'superseded')",
            name=op.f("ck_sms_billing_usage_reports_status"),
        ),
        sa.CheckConstraint(
            "report_seq > 0", name=op.f("ck_sms_billing_usage_reports_seq_positive")
        ),
        sa.CheckConstraint(
            "watermark >= 0 AND cumulative_billable_count >= 0 AND cumulative_billable_count <= watermark",
            name=op.f("ck_sms_billing_usage_reports_counts"),
        ),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_sms_billing_usage_reports")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "report_seq", name="uq_sms_billing_usage_reports_seq"
        ),
    )
    op.create_index(
        "ix_sms_billing_usage_reports_status_next",
        "sms_billing_usage_reports",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_table(
        "sms_email_billable_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("email_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("first_status", sa.String(length=16), nullable=False),
        sa.Column("billable_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "first_status in ('sent', 'delivered', 'bounced', 'complained')",
            name=op.f("ck_sms_email_billable_events_first_status"),
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["sms_emails.id"],
            name=op.f("fk_sms_email_billable_events_email_id_sms_emails"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_email_billable_events")),
        sa.UniqueConstraint("email_id", name=op.f("uq_sms_email_billable_events_email_id")),
    )
    op.create_index(
        op.f("ix_sms_email_billable_events_enterprise_id"),
        "sms_email_billable_events",
        ["enterprise_id"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_billing_evidence_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_email_billable_events_immutable BEFORE UPDATE OR DELETE ON sms_email_billable_events FOR EACH ROW EXECUTE FUNCTION sms_billing_evidence_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_email_billable_events_no_truncate BEFORE TRUNCATE ON sms_email_billable_events FOR EACH STATEMENT EXECUTE FUNCTION sms_billing_evidence_forbid()"
        )
        cols = ("report_id", "enterprise_id", "product_id", "report_seq", "watermark", "cumulative_billable_count",
                "generated_at", "payload_hash", "content_hash", "created_at")  # fmt: skip
        cond = (
            " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)
            + " OR NEW.payload::text IS DISTINCT FROM OLD.payload::text"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_billing_usage_report_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION 'billing usage report content is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_billing_usage_reports_guard BEFORE UPDATE ON sms_billing_usage_reports FOR EACH ROW EXECUTE FUNCTION sms_billing_usage_report_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_billing_usage_reports_no_delete BEFORE DELETE ON sms_billing_usage_reports FOR EACH ROW EXECUTE FUNCTION sms_billing_evidence_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sms_billing_usage_reports_no_delete ON sms_billing_usage_reports",
            "DROP TRIGGER IF EXISTS trg_sms_billing_usage_reports_guard ON sms_billing_usage_reports",
            "DROP FUNCTION IF EXISTS sms_billing_usage_report_guard()",
            "DROP TRIGGER IF EXISTS trg_sms_email_billable_events_no_truncate ON sms_email_billable_events",
            "DROP TRIGGER IF EXISTS trg_sms_email_billable_events_immutable ON sms_email_billable_events",
            "DROP FUNCTION IF EXISTS sms_billing_evidence_forbid()",
        ):
            op.execute(stmt)
    op.drop_index(
        op.f("ix_sms_email_billable_events_enterprise_id"), table_name="sms_email_billable_events"
    )
    op.drop_table("sms_email_billable_events")
    op.drop_index(
        "ix_sms_billing_usage_reports_status_next", table_name="sms_billing_usage_reports"
    )
    op.drop_table("sms_billing_usage_reports")
