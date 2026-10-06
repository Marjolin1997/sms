"""Central: faturimi periodik g1 (M9-g). Aditiv.

commercial_plans, plan_versions, billing_profiles, billing_subscriptions, invoice_number_sequence, invoices,
invoice_lines, billing_periods. PostgreSQL: trigger-a që e bëjnë të pandryshueshme versionin e aktivizuar të planit, faturën
(fushat financiare, tranzicionet open→paid|void), linjat dhe periudhat; refuzojnë DELETE; dhe një constraint trigger të shtyrë që
e verifikon aritmetikën (subtotal = Σ linja, tax = round(subtotal × vat, 2), total) në commit.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invoice_number_sequence",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "last_number >= 0", name=op.f("ck_invoice_number_sequence_non_negative")
        ),
        sa.PrimaryKeyConstraint("year", name=op.f("pk_invoice_number_sequence")),
    )
    op.create_table(
        "billing_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(length=120), nullable=False),
        sa.Column("address", sa.String(length=300), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("tax_id", sa.String(length=40), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("vat_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(country) = 2", name=op.f("ck_billing_profiles_country")),
        sa.CheckConstraint(
            "vat_rate >= 0 AND vat_rate <= 1", name=op.f("ck_billing_profiles_vat_range")
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_profiles_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_profiles")),
        sa.UniqueConstraint("enterprise_id", name=op.f("uq_billing_profiles_enterprise_id")),
    )
    op.create_table(
        "commercial_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_commercial_plans_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_plans")),
        sa.UniqueConstraint("code", name=op.f("uq_commercial_plans_code")),
    )
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("monthly_fee", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("included_emails", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by_id", sa.Uuid(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_by_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "(status = 'draft' AND content_hash IS NULL AND activated_at IS NULL) OR (status <> 'draft' AND content_hash IS NOT NULL AND activated_at IS NOT NULL)",
            name=op.f("ck_plan_versions_status_consistency"),
        ),
        sa.CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL AND retire_reason IS NOT NULL) OR (status <> 'retired' AND retired_at IS NULL AND retire_reason IS NULL)",
            name=op.f("ck_plan_versions_retire_consistency"),
        ),
        sa.CheckConstraint(
            "status in ('draft', 'active', 'retired')", name=op.f("ck_plan_versions_status")
        ),
        sa.CheckConstraint(
            "included_emails >= 0", name=op.f("ck_plan_versions_included_non_negative")
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_plan_versions_currency"),
        ),
        sa.CheckConstraint("monthly_fee >= 0", name=op.f("ck_plan_versions_fee_non_negative")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_plan_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["activated_by_id"],
            ["users.id"],
            name=op.f("fk_plan_versions_activated_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_plan_versions_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["commercial_plans.id"],
            name=op.f("fk_plan_versions_plan_id_commercial_plans"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_id"],
            ["users.id"],
            name=op.f("fk_plan_versions_retired_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_versions")),
        sa.UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_version"),
    )
    op.create_index(
        "uq_plan_versions_one_draft",
        "plan_versions",
        ["plan_id"],
        unique=True,
        postgresql_where=sa.text("status = 'draft'"),
        sqlite_where=sa.text("status = 'draft'"),
    )
    op.create_table(
        "billing_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("pending_plan_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("anchor_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("anchor_period_index", sa.Integer(), nullable=False),
        sa.Column("next_period_index", sa.Integer(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR (status = 'active' AND cancelled_at IS NULL)",
            name=op.f("ck_billing_subscriptions_cancel_consistency"),
        ),
        sa.CheckConstraint(
            "status in ('active', 'cancelled')", name=op.f("ck_billing_subscriptions_status")
        ),
        sa.CheckConstraint(
            "anchor_period_index >= 0 AND next_period_index >= anchor_period_index",
            name=op.f("ck_billing_subscriptions_indexes"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_subscriptions_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pending_plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_billing_subscriptions_pending_plan_version_id_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_billing_subscriptions_plan_version_id_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_subscriptions")),
        sa.UniqueConstraint("enterprise_id", name=op.f("uq_billing_subscriptions_enterprise_id")),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=24), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("period_index", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("vat_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("tax", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("bill_to", sa.JSON(), nullable=False),
        sa.Column("issuer", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by_id", sa.Uuid(), nullable=True),
        sa.Column("voided_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'open' AND paid_at IS NULL AND voided_at IS NULL AND voided_reason IS NULL) OR (status = 'paid' AND paid_at IS NOT NULL AND voided_at IS NULL AND voided_reason IS NULL) OR (status = 'void' AND paid_at IS NULL AND voided_at IS NOT NULL AND voided_reason IS NOT NULL AND length(trim(voided_reason)) > 0)",
            name=op.f("ck_invoices_status_consistency"),
        ),
        sa.CheckConstraint("status in ('open', 'paid', 'void')", name=op.f("ck_invoices_status")),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)", name=op.f("ck_invoices_currency")
        ),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_invoices_period_order")),
        sa.CheckConstraint(
            "subtotal >= 0 AND tax >= 0 AND total = subtotal + tax",
            name=op.f("ck_invoices_arithmetic"),
        ),
        sa.CheckConstraint("vat_rate >= 0 AND vat_rate <= 1", name=op.f("ck_invoices_vat_range")),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_invoices_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_invoices_plan_version_id_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_invoices_subscription_id_billing_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["voided_by_id"],
            ["users.id"],
            name=op.f("fk_invoices_voided_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("id", "currency", name="uq_invoices_id_currency"),
        sa.UniqueConstraint("number", name=op.f("uq_invoices_number")),
        sa.UniqueConstraint("subscription_id", "period_index", name="uq_invoices_sub_period_index"),
        sa.UniqueConstraint("subscription_id", "period_start", name="uq_invoices_sub_period_start"),
    )
    op.create_index(
        "ix_invoices_enterprise", "invoices", ["enterprise_id", "issued_at"], unique=False
    )
    op.create_index("ix_invoices_status_due", "invoices", ["status", "due_at"], unique=False)
    op.create_table(
        "billing_periods",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("period_index", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("billed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usage_from", sa.BigInteger(), nullable=True),
        sa.Column("usage_to", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'invoiced' AND invoice_id IS NOT NULL) OR (status = 'no_charge' AND invoice_id IS NULL)",
            name=op.f("ck_billing_periods_invoice_consistency"),
        ),
        sa.CheckConstraint(
            "status in ('invoiced', 'no_charge')", name=op.f("ck_billing_periods_status")
        ),
        sa.CheckConstraint(
            "period_end > period_start", name=op.f("ck_billing_periods_period_order")
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_periods_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f("fk_billing_periods_invoice_id_invoices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_billing_periods_plan_version_id_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_billing_periods_subscription_id_billing_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_periods")),
        sa.UniqueConstraint("invoice_id", name="uq_billing_periods_invoice"),
        sa.UniqueConstraint("subscription_id", "period_index", name="uq_billing_periods_index"),
        sa.UniqueConstraint("subscription_id", "period_start", name="uq_billing_periods_start"),
    )
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("line_type", sa.String(length=16), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_version_id", sa.Uuid(), nullable=True),
        sa.Column("pricing_source", sa.String(length=16), nullable=True),
        sa.Column("price_book_id", sa.Uuid(), nullable=True),
        sa.Column("price_version_id", sa.Uuid(), nullable=True),
        sa.Column("price_rule_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "line_type in ('monthly_fee', 'email_overage', 'adjustment')",
            name=op.f("ck_invoice_lines_line_type"),
        ),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_invoice_lines_period_order")),
        sa.CheckConstraint(
            "quantity > 0 AND unit_price >= 0 AND amount >= 0",
            name=op.f("ck_invoice_lines_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id", "currency"],
            ["invoices.id", "invoices.currency"],
            name="fk_invoice_lines_invoice_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_version_id"],
            ["plan_versions.id"],
            name=op.f("fk_invoice_lines_plan_version_id_plan_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_book_id"],
            ["price_books.id"],
            name=op.f("fk_invoice_lines_price_book_id_price_books"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_rule_id"],
            ["price_rules.id"],
            name=op.f("fk_invoice_lines_price_rule_id_price_rules"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_version_id"],
            ["price_versions.id"],
            name=op.f("fk_invoice_lines_price_version_id_price_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_lines")),
        sa.UniqueConstraint("invoice_id", "line_no", name="uq_invoice_lines_no"),
    )
    op.create_index("ix_invoice_lines_invoice", "invoice_lines", ["invoice_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        _triggers()


def _triggers() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION central_billing_forbid() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    for t in ("commercial_plans", "plan_versions", "billing_subscriptions", "billing_profiles", "invoices",
              "invoice_lines", "billing_periods", "invoice_number_sequence"):  # fmt: skip
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
        )
    for t in ("commercial_plans", "plan_versions", "billing_subscriptions", "billing_profiles", "invoices",
              "invoice_lines", "billing_periods", "invoice_number_sequence"):  # fmt: skip
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION central_billing_forbid()"
        )
    for t in ("invoice_lines", "billing_periods"):
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_update BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
        )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_commercial_plans_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.code IS DISTINCT FROM OLD.code OR NEW.name IS DISTINCT FROM OLD.name "
        "OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN "
        "RAISE EXCEPTION 'commercial plan is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_commercial_plans_guard BEFORE UPDATE ON commercial_plans FOR EACH ROW EXECUTE FUNCTION central_commercial_plans_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_plan_versions_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.plan_id IS DISTINCT FROM OLD.plan_id OR NEW.version IS DISTINCT FROM OLD.version "
        "OR NEW.created_by_id IS DISTINCT FROM OLD.created_by_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN "
        "RAISE EXCEPTION 'plan version identity is immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'draft' AND (NEW.currency IS DISTINCT FROM OLD.currency OR NEW.monthly_fee IS DISTINCT FROM OLD.monthly_fee "
        "OR NEW.included_emails IS DISTINCT FROM OLD.included_emails OR NEW.content_hash IS DISTINCT FROM OLD.content_hash "
        "OR NEW.activated_at IS DISTINCT FROM OLD.activated_at OR NEW.activated_by_id IS DISTINCT FROM OLD.activated_by_id) THEN "
        "RAISE EXCEPTION 'an activated plan version is immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM OLD.status THEN RAISE EXCEPTION 'retired is final' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'active' AND NEW.status NOT IN ('active', 'retired') THEN RAISE EXCEPTION 'an active version can only be retired' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'active') THEN RAISE EXCEPTION 'a draft can only be activated' USING ERRCODE = '55000'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_plan_versions_guard BEFORE UPDATE ON plan_versions FOR EACH ROW EXECUTE FUNCTION central_plan_versions_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_billing_subscriptions_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.enterprise_id IS DISTINCT FROM OLD.enterprise_id OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN "
        "RAISE EXCEPTION 'subscription identity is immutable' USING ERRCODE = '55000'; END IF; "
        "IF NEW.next_period_index < OLD.next_period_index AND NEW.anchor_period_index IS NOT DISTINCT FROM OLD.anchor_period_index THEN "
        "RAISE EXCEPTION 'next_period_index never goes back' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_billing_subscriptions_guard BEFORE UPDATE ON billing_subscriptions FOR EACH ROW EXECUTE FUNCTION central_billing_subscriptions_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_invoices_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.number IS DISTINCT FROM OLD.number OR NEW.enterprise_id IS DISTINCT FROM OLD.enterprise_id "
        "OR NEW.subscription_id IS DISTINCT FROM OLD.subscription_id OR NEW.period_index IS DISTINCT FROM OLD.period_index "
        "OR NEW.period_start IS DISTINCT FROM OLD.period_start OR NEW.period_end IS DISTINCT FROM OLD.period_end "
        "OR NEW.plan_version_id IS DISTINCT FROM OLD.plan_version_id OR NEW.currency IS DISTINCT FROM OLD.currency "
        "OR NEW.subtotal IS DISTINCT FROM OLD.subtotal OR NEW.vat_rate IS DISTINCT FROM OLD.vat_rate OR NEW.tax IS DISTINCT FROM OLD.tax "
        "OR NEW.total IS DISTINCT FROM OLD.total OR NEW.bill_to::text IS DISTINCT FROM OLD.bill_to::text "
        "OR NEW.issuer::text IS DISTINCT FROM OLD.issuer::text OR NEW.issued_at IS DISTINCT FROM OLD.issued_at "
        "OR NEW.due_at IS DISTINCT FROM OLD.due_at OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN "
        "RAISE EXCEPTION 'invoice % is immutable', OLD.number USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'open' AND (NEW.status IS DISTINCT FROM OLD.status OR NEW.paid_at IS DISTINCT FROM OLD.paid_at "
        "OR NEW.voided_at IS DISTINCT FROM OLD.voided_at OR NEW.voided_by_id IS DISTINCT FROM OLD.voided_by_id "
        "OR NEW.voided_reason IS DISTINCT FROM OLD.voided_reason) THEN "
        "RAISE EXCEPTION 'invoice % status is final', OLD.number USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_invoices_guard BEFORE UPDATE ON invoices FOR EACH ROW EXECUTE FUNCTION central_invoices_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_invoice_number_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.year IS DISTINCT FROM OLD.year OR NEW.last_number < OLD.last_number THEN "
        "RAISE EXCEPTION 'invoice number sequence only moves forward' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_invoice_number_sequence_guard BEFORE UPDATE ON invoice_number_sequence FOR EACH ROW EXECUTE FUNCTION central_invoice_number_guard()"
    )
    # aritmetika e faturës (e shtyrë deri në commit): subtotal = Σ linja, linja = round(sasia × çmimi, 2), tax = round(subtotal × vat, 2)
    op.execute(
        "CREATE OR REPLACE FUNCTION central_invoice_arithmetic() RETURNS trigger AS $$ "
        "DECLARE inv_id uuid; s numeric; n integer; bad integer; r invoices%ROWTYPE; BEGIN "
        "IF TG_TABLE_NAME = 'invoices' THEN inv_id := NEW.id; ELSE inv_id := NEW.invoice_id; END IF; "
        "SELECT * INTO r FROM invoices WHERE id = inv_id; IF NOT FOUND THEN RETURN NULL; END IF; "
        "SELECT coalesce(sum(amount), 0), count(*), count(*) FILTER (WHERE amount <> round(quantity * unit_price, 2)) INTO s, n, bad "
        "FROM invoice_lines WHERE invoice_id = inv_id; "
        "IF n = 0 THEN RAISE EXCEPTION 'invoice % has no lines', r.number USING ERRCODE = '23514'; END IF; "
        "IF bad > 0 THEN RAISE EXCEPTION 'invoice % has a line whose amount is not round(quantity*unit_price, 2)', r.number USING ERRCODE = '23514'; END IF; "
        "IF r.subtotal <> s OR r.tax <> round(r.subtotal * r.vat_rate, 2) OR r.total <> r.subtotal + r.tax THEN "
        "RAISE EXCEPTION 'invoice % totals are not derived from its lines', r.number USING ERRCODE = '23514'; END IF; "
        "RETURN NULL; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_invoices_arithmetic AFTER INSERT ON invoices DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION central_invoice_arithmetic()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_invoice_lines_arithmetic AFTER INSERT ON invoice_lines DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION central_invoice_arithmetic()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_invoice_lines_arithmetic ON invoice_lines",
            "DROP TRIGGER IF EXISTS trg_invoices_arithmetic ON invoices",
            "DROP FUNCTION IF EXISTS central_invoice_arithmetic()",
            "DROP TRIGGER IF EXISTS trg_invoice_number_sequence_guard ON invoice_number_sequence",
            "DROP FUNCTION IF EXISTS central_invoice_number_guard()",
            "DROP TRIGGER IF EXISTS trg_invoices_guard ON invoices",
            "DROP FUNCTION IF EXISTS central_invoices_guard()",
            "DROP TRIGGER IF EXISTS trg_billing_subscriptions_guard ON billing_subscriptions",
            "DROP FUNCTION IF EXISTS central_billing_subscriptions_guard()",
            "DROP TRIGGER IF EXISTS trg_plan_versions_guard ON plan_versions",
            "DROP FUNCTION IF EXISTS central_plan_versions_guard()",
            "DROP TRIGGER IF EXISTS trg_commercial_plans_guard ON commercial_plans",
            "DROP FUNCTION IF EXISTS central_commercial_plans_guard()",
            "DROP TRIGGER IF EXISTS trg_billing_periods_no_update ON billing_periods",
            "DROP TRIGGER IF EXISTS trg_invoice_lines_no_update ON invoice_lines",
        ):
            op.execute(stmt)
        for t in ("commercial_plans", "plan_versions", "billing_subscriptions", "billing_profiles", "invoices",
                  "invoice_lines", "billing_periods", "invoice_number_sequence"):  # fmt: skip
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_delete ON {t}")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_truncate ON {t}")
        op.execute("DROP FUNCTION IF EXISTS central_billing_forbid()")
    op.drop_index("ix_invoice_lines_invoice", table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_table("billing_periods")
    op.drop_index("ix_invoices_status_due", table_name="invoices")
    op.drop_index("ix_invoices_enterprise", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("billing_subscriptions")
    op.drop_index(
        "uq_plan_versions_one_draft",
        table_name="plan_versions",
        postgresql_where=sa.text("status = 'draft'"),
        sqlite_where=sa.text("status = 'draft'"),
    )
    op.drop_table("plan_versions")
    op.drop_table("commercial_plans")
    op.drop_table("billing_profiles")
    op.drop_table("invoice_number_sequence")
