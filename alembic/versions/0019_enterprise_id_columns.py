"""M1b: kolona `enterprise_id` (nullable, e indeksuar) në 21 tabelat me `owner_ref`

ADITIVE. Nuk ka backfill këtu (bëhet në batch nga `python -m scripts.backfill_enterprise_id`), nuk ka
FK dhe nuk ka NOT NULL (M1c pas verifikimit). Asnjë sjellje nuk ndryshon: asgjë nuk lexon kolonën.

PostgreSQL:
  * `SET LOCAL lock_timeout`: ALTER TABLE ADD COLUMN nullable pa default është vetëm metadata (çast),
    por kërkon kyçje të shkurtër ACCESS EXCLUSIVE; me lock_timeout dështon shpejt në vend që të bllokojë
    trafikun në radhë pas një transaksioni të gjatë (riprovo migrimin).
  * Indekset krijohen `CREATE INDEX CONCURRENTLY IF NOT EXISTS` (pa bllokuar shkrimet), jashtë transaksionit.
  * `sms_consent_events` është i pandryshueshëm (trigger). Trigger-i zëvendësohet që të lejojë VETËM
    UPDATE që ndryshon vetëm `enterprise_id` nga NULL në vlerë (backfill); çdo ndryshim tjetër dhe çdo
    DELETE mbeten të ndaluara (prova e pandryshueshmërisë mbetet e plotë, shih testin).

Rikthimi: downgrade heq indekset dhe kolonat (të dhënat `owner_ref` të paprekura) dhe rikthen
trigger-in origjinal.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# Kopje e ngrirë e LEGACY_OWNER_TABLES (testi i driftit e krahason me modelet)
TABLES = (
    "sms_account_plans", "sms_api_keys", "sms_billing_profiles", "sms_campaigns",
    "sms_consent_events", "sms_consent_state", "sms_contact_lists", "sms_contacts",
    "sms_email_domains", "sms_emails", "sms_events", "sms_inbound_messages", "sms_invoices",
    "sms_keywords", "sms_messages", "sms_payments", "sms_sender_ids", "sms_subscriptions",
    "sms_templates", "sms_wallets", "sms_webhook_endpoints",
)  # fmt: skip


def _index(table: str) -> str:
    return f"ix_{table}_enterprise_id"  # sipas NAMING["ix"] = ix_<tabela>_<kolona>


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    bind = op.get_bind()
    if _is_pg():
        op.execute("SET LOCAL lock_timeout = '5s'")
    have = set(sa.inspect(bind).get_table_names())
    for t in TABLES:
        if t not in have:
            continue
        cols = {c["name"] for c in sa.inspect(bind).get_columns(t)}
        if "enterprise_id" not in cols:
            op.add_column(t, sa.Column("enterprise_id", sa.Uuid(), nullable=True))
    if _is_pg():
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_consent_guard() RETURNS trigger AS $$ BEGIN "
            "IF OLD.enterprise_id IS NULL AND NEW.enterprise_id IS NOT NULL "
            "AND (to_jsonb(OLD) - 'enterprise_id') = (to_jsonb(NEW) - 'enterprise_id') "
            "THEN RETURN NEW; END IF; "
            "RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; "
            "END; $$ LANGUAGE plpgsql"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_sms_consent_immutable ON sms_consent_events")
        op.execute(
            "CREATE TRIGGER trg_sms_consent_immutable BEFORE UPDATE ON sms_consent_events "
            "FOR EACH ROW EXECUTE FUNCTION sms_consent_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_consent_no_delete BEFORE DELETE ON sms_consent_events "
            "FOR EACH ROW EXECUTE FUNCTION sms_forbid_mutation()"
        )
        with op.get_context().autocommit_block():  # jashtë transaksionit: kërkesë e CONCURRENTLY
            for t in TABLES:
                if t in have:
                    op.execute(
                        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_index(t)} ON {t} (enterprise_id)"
                    )
    else:
        for t in TABLES:
            if t in have:
                op.create_index(_index(t), t, ["enterprise_id"])


def downgrade() -> None:
    bind = op.get_bind()
    have = set(sa.inspect(bind).get_table_names())
    if _is_pg():
        op.execute("DROP TRIGGER IF EXISTS trg_sms_consent_no_delete ON sms_consent_events")
        op.execute("DROP TRIGGER IF EXISTS trg_sms_consent_immutable ON sms_consent_events")
        op.execute(
            "CREATE TRIGGER trg_sms_consent_immutable BEFORE UPDATE OR DELETE ON sms_consent_events "
            "FOR EACH ROW EXECUTE FUNCTION sms_forbid_mutation()"
        )
        op.execute("DROP FUNCTION IF EXISTS sms_consent_guard()")
    for t in reversed(TABLES):
        if t not in have:
            continue
        if not _is_pg():
            op.drop_index(_index(t), table_name=t)
        else:
            op.execute(f"DROP INDEX IF EXISTS {_index(t)}")
        with op.batch_alter_table(t) as b:
            b.drop_column("enterprise_id")
