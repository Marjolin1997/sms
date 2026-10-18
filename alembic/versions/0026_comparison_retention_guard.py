"""Enterprise: retention i kontrolluar për `sms_pricing_comparisons` dhe outbox-in `sms_usage_reports` (M9-f). Pa ndryshim skeme.

Të dyja mbeten të pandryshueshme (UPDATE i përmbajtjes ndalohet gjithmonë). DELETE lejohet VETËM në një transaksion që
ka vendosur shprehimisht `sms.retention_delete = on` (mjeti `scripts.financial_retention`, dry-run parazgjedhje).

Revision ID: 0026
Revises: 0025
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_pricing_comparisons_guard() RETURNS trigger AS $$ "
        "BEGIN IF TG_OP = 'DELETE' AND current_setting('sms.retention_delete', true) = 'on' THEN RETURN OLD; END IF; "
        "RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sms_pricing_comparisons_immutable ON sms_pricing_comparisons"
    )
    op.execute(
        "CREATE TRIGGER trg_sms_pricing_comparisons_immutable BEFORE UPDATE OR DELETE ON sms_pricing_comparisons "
        "FOR EACH ROW EXECUTE FUNCTION sms_pricing_comparisons_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_usage_reports_delete_guard() RETURNS trigger AS $$ "
        "BEGIN IF current_setting('sms.retention_delete', true) = 'on' THEN RETURN OLD; END IF; "
        "RAISE EXCEPTION '% rows are never deleted outside retention', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_sms_usage_reports_no_delete ON sms_usage_reports")
    op.execute(
        "CREATE TRIGGER trg_sms_usage_reports_no_delete BEFORE DELETE ON sms_usage_reports "
        "FOR EACH ROW EXECUTE FUNCTION sms_usage_reports_delete_guard()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "DROP TRIGGER IF EXISTS trg_sms_pricing_comparisons_immutable ON sms_pricing_comparisons"
    )
    op.execute(
        "CREATE TRIGGER trg_sms_pricing_comparisons_immutable BEFORE UPDATE OR DELETE ON sms_pricing_comparisons "
        "FOR EACH ROW EXECUTE FUNCTION sms_pricing_forbid()"
    )
    op.execute("DROP FUNCTION IF EXISTS sms_pricing_comparisons_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_sms_usage_reports_no_delete ON sms_usage_reports")
    op.execute(
        "CREATE TRIGGER trg_sms_usage_reports_no_delete BEFORE DELETE ON sms_usage_reports "
        "FOR EACH ROW EXECUTE FUNCTION sms_money_forbid_delete()"
    )
    op.execute("DROP FUNCTION IF EXISTS sms_usage_reports_delete_guard()")
