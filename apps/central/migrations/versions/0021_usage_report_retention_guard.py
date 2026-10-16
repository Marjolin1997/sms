"""Central: retention i kontrolluar për `usage_reports` (M9-f). Pa ndryshim skeme.

`usage_reports` mbetet vetëm-shtim: UPDATE/TRUNCATE ndalohen gjithmonë. DELETE lejohet VETËM brenda një
transaksioni që e ka vendosur shprehimisht `central.retention_delete = on` (mjeti `apps.central.tools.retention`,
që mban raportin aktual per çelës, `keep_last` të fundit dhe një per ditë UTC në dritaren e ndërmjetme).

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION central_usage_reports_guard() RETURNS trigger AS $$ "
        "BEGIN IF TG_OP = 'DELETE' AND current_setting('central.retention_delete', true) = 'on' THEN RETURN OLD; END IF; "
        "RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_usage_reports_immutable ON usage_reports")
    op.execute(
        "CREATE TRIGGER trg_usage_reports_immutable BEFORE UPDATE OR DELETE ON usage_reports "
        "FOR EACH ROW EXECUTE FUNCTION central_usage_reports_guard()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_usage_reports_immutable ON usage_reports")
    op.execute(
        "CREATE TRIGGER trg_usage_reports_immutable BEFORE UPDATE OR DELETE ON usage_reports "
        "FOR EACH ROW EXECUTE FUNCTION central_money_forbid_mutation()"
    )
    op.execute("DROP FUNCTION IF EXISTS central_usage_reports_guard()")
