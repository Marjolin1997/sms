"""Append-only në nivel databaze për historikun e statuseve dhe DLR receipts.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

TABLES = {
    "sms_message_events": ("trg_sms_msg_events_immutable", "trg_sms_msg_events_no_truncate"),
    "sms_dlr_receipts": ("trg_sms_dlr_immutable", "trg_sms_dlr_no_truncate"),
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_forbid_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; "
        "END; $$ LANGUAGE plpgsql"
    )
    for table, (row_trg, trunc_trg) in TABLES.items():
        op.execute(
            f"CREATE TRIGGER {row_trg} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sms_forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {trunc_trg} BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION sms_forbid_mutation()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, (row_trg, trunc_trg) in TABLES.items():
        op.execute(f"DROP TRIGGER IF EXISTS {row_trg} ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {trunc_trg} ON {table}")
