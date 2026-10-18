"""Enterprise: snapshot i identitetit të lëshuesit te faturat (M11-E2.1). Aditiv: një kolonë nullable.

`sms_invoices.issuer` (Text, JSON `{name,address,tax_id}`) ruan lëshuesin në çastin e krijimit, simetrik me `bill_to`.
NULL = faturë "legacy" (para këtij migrimi): NUK plotësohet prapa nga konfigurimi aktual (histori e panjohur); renderohet
me fallback eksplicit te `SMS_ISSUER_*`. Pa indeks, pa NOT NULL, pa rishkrim tabele.
PostgreSQL: roja e pandryshueshmërisë (`sms_invoice_guard`) shtrihet te `issuer` (çdo ndryshim, përfshirë NULL→vlerë, refuzohet).

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

BASE = ("number", "owner_ref", "subscription_id", "period_start", "period_end", "currency", "subtotal",
        "vat_rate", "tax", "total", "bill_to", "issued_at", "due_at")  # fmt: skip


def _guard(cols: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_invoice_guard() RETURNS trigger AS $$ BEGIN "
        f"IF {cond} THEN RAISE EXCEPTION 'invoice % is immutable', OLD.number "
        "USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'OPEN' AND NEW.status IS DISTINCT FROM OLD.status THEN "
        "RAISE EXCEPTION 'invoice % status is final', OLD.number USING ERRCODE = '55000'; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    op.add_column("sms_invoices", sa.Column("issuer", sa.Text(), nullable=True))
    _guard((*BASE, "issuer"))


def downgrade() -> None:
    _guard(BASE)
    op.drop_column("sms_invoices", "issuer")
