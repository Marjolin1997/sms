"""Central: veprimet e kërkesës së sender-it nga Enterprise (`sender.request.v1`, M10-S3). Aditiv.

`sender_request_operations`: një rresht për çdo veprim logjik të pranuar (`operation_id` UNIQUE = dedupe i transportit at-least-once). Append-only (PostgreSQL: trigger).
Asnjë ndryshim i tabelave ekzistuese.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_request_operations",
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=8), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("auto", sa.String(length=16), nullable=False),
        sa.Column("status_after", sa.String(length=12), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation in ('request', 'resubmit')",
            name=op.f("ck_sender_request_operations_operation"),
        ),
        sa.CheckConstraint(
            "outcome in ('created', 'existing', 'resubmitted', 'noop_pending', 'noop_approved')",
            name=op.f("ck_sender_request_operations_outcome"),
        ),
        sa.CheckConstraint(
            "auto in ('not_applicable', 'approved', 'denied', 'blocked')",
            name=op.f("ck_sender_request_operations_auto"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_sender_request_operations_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["sender_registry.id"],
            name=op.f("fk_sender_request_operations_registry_id_sender_registry"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_sender_request_operations")),
    )
    op.create_index(
        "ix_sender_request_operations_registry",
        "sender_request_operations",
        ["registry_id", "received_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION central_sender_request_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_request_operations_immutable BEFORE UPDATE OR DELETE ON sender_request_operations FOR EACH ROW EXECUTE FUNCTION central_sender_request_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_request_operations_no_truncate BEFORE TRUNCATE ON sender_request_operations FOR EACH STATEMENT EXECUTE FUNCTION central_sender_request_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sender_request_operations_no_truncate ON sender_request_operations",
            "DROP TRIGGER IF EXISTS trg_sender_request_operations_immutable ON sender_request_operations",
            "DROP FUNCTION IF EXISTS central_sender_request_forbid()",
        ):
            op.execute(stmt)
    op.drop_index("ix_sender_request_operations_registry", table_name="sender_request_operations")
    op.drop_table("sender_request_operations")
