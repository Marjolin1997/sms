"""Central: raportet kumulative të email-eve të faturueshme + prova e deltës në periudhë (M9-g2). Aditiv.

`billing_usage_reports` (append-only; trigger PG që refuzon UPDATE/DELETE/TRUNCATE) dhe dy kolona FK te `billing_periods`
(`usage_from_report_id`, `usage_to_report_id`) + CHECK i rendit të numëruesve.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_usage_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("report_seq", sa.BigInteger(), nullable=False),
        sa.Column("watermark", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_billable_count", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.CheckConstraint("report_seq > 0", name=op.f("ck_billing_usage_reports_seq_positive")),
        sa.CheckConstraint(
            "watermark >= 0 AND cumulative_billable_count >= 0 AND cumulative_billable_count <= watermark",
            name=op.f("ck_billing_usage_reports_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_usage_reports_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_billing_usage_reports_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_billing_usage_reports")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "report_seq", name="uq_billing_usage_reports_seq"
        ),
    )
    op.create_index(
        "ix_billing_usage_reports_generated",
        "billing_usage_reports",
        ["enterprise_id", "product_id", "generated_at"],
        unique=False,
    )
    with op.batch_alter_table("billing_periods") as batch:
        batch.add_column(sa.Column("usage_from_report_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("usage_to_report_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_billing_periods_usage_from_report_id_billing_usage_reports"),
            "billing_usage_reports",
            ["usage_from_report_id"],
            ["report_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            op.f("fk_billing_periods_usage_to_report_id_billing_usage_reports"),
            "billing_usage_reports",
            ["usage_to_report_id"],
            ["report_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            op.f("ck_billing_periods_usage_order"),
            "usage_from IS NULL OR (usage_to IS NOT NULL AND usage_to >= usage_from AND usage_from >= 0)",
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION central_billing_usage_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_usage_reports_immutable BEFORE UPDATE OR DELETE ON billing_usage_reports "
            "FOR EACH ROW EXECUTE FUNCTION central_billing_usage_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_billing_usage_reports_no_truncate BEFORE TRUNCATE ON billing_usage_reports "
            "FOR EACH STATEMENT EXECUTE FUNCTION central_billing_usage_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_billing_usage_reports_no_truncate ON billing_usage_reports"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_billing_usage_reports_immutable ON billing_usage_reports"
        )
        op.execute("DROP FUNCTION IF EXISTS central_billing_usage_forbid()")
    with op.batch_alter_table("billing_periods") as batch:
        batch.drop_constraint(op.f("ck_billing_periods_usage_order"), type_="check")
        batch.drop_constraint(
            op.f("fk_billing_periods_usage_to_report_id_billing_usage_reports"), type_="foreignkey"
        )
        batch.drop_constraint(
            op.f("fk_billing_periods_usage_from_report_id_billing_usage_reports"),
            type_="foreignkey",
        )
        batch.drop_column("usage_to_report_id")
        batch.drop_column("usage_from_report_id")
    op.drop_index("ix_billing_usage_reports_generated", table_name="billing_usage_reports")
    op.drop_table("billing_usage_reports")
