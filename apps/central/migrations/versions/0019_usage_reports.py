"""Central: raportet e përdorimit financiar (M9-d). Aditiv, i pandryshueshëm.

`usage_reports`: ruhet çdo raport kumulativ i marrë nga Enterprise (payload kanonik + kolona për indekse).
PostgreSQL: trigger-at e `0017` (`central_money_forbid_mutation`) refuzojnë UPDATE/DELETE/TRUNCATE.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 6)


def upgrade() -> None:
    op.create_table(
        "usage_reports",
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("report_seq", sa.BigInteger(), nullable=False),
        sa.Column("authority_mode", sa.String(length=8), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ledger_max_id", sa.BigInteger(), nullable=False),
        sa.Column("money_cursor_seq", sa.BigInteger(), nullable=False),
        sa.Column("money_cursor_epoch", sa.Uuid(), nullable=True),
        sa.Column("available", MONEY, nullable=False),
        sa.Column("held", MONEY, nullable=False),
        sa.Column("gross", MONEY, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_usage_reports_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_usage_reports_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("report_id", name=op.f("pk_usage_reports")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "currency", "report_seq", name="uq_usage_reports_seq"
        ),
        sa.CheckConstraint("report_seq > 0", name=op.f("ck_usage_reports_seq_positive")),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_usage_reports_currency"),
        ),
        sa.CheckConstraint(
            "authority_mode in ('local', 'shadow', 'central')",
            name=op.f("ck_usage_reports_authority_mode"),
        ),
        sa.CheckConstraint(
            "abs(gross - available - held) < 0.0000005", name=op.f("ck_usage_reports_gross")
        ),
    )
    op.create_index(
        "ix_usage_reports_enterprise_generated", "usage_reports", ["enterprise_id", "generated_at"]
    )
    op.create_index(
        "ix_usage_reports_key_ledger", "usage_reports",
        ["enterprise_id", "product_id", "currency", "ledger_max_id"],
    )  # fmt: skip
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER trg_usage_reports_immutable BEFORE UPDATE OR DELETE ON usage_reports "
            "FOR EACH ROW EXECUTE FUNCTION central_money_forbid_mutation()"
        )
        op.execute(
            "CREATE TRIGGER trg_usage_reports_no_truncate BEFORE TRUNCATE ON usage_reports "
            "FOR EACH STATEMENT EXECUTE FUNCTION central_money_forbid_mutation()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_usage_reports_no_truncate ON usage_reports")
        op.execute("DROP TRIGGER IF EXISTS trg_usage_reports_immutable ON usage_reports")
    op.drop_index("ix_usage_reports_key_ledger", table_name="usage_reports")
    op.drop_index("ix_usage_reports_enterprise_generated", table_name="usage_reports")
    op.drop_table("usage_reports")
