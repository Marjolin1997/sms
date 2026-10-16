"""Central: shlyerja e faturave (M9-g3). Aditiv.

`payments` merr `purpose` (credit|invoice, default credit) dhe `invoice_id`; `account_id` bëhet nullable (CHECK: purpose=credit ⇒ account_id NOT NULL dhe
invoice_id NULL; purpose=invoice ⇒ account_id NULL dhe invoice_id NOT NULL). Tabela të reja: `invoice_payment_allocations`, `credit_notes`,
`credit_note_sequence`. PostgreSQL: guard i pagesave zgjerohet (purpose/invoice_id të ngrira), trigger-a immutability për tabelat e reja dhe
constraint trigger-a të shtyrë (paid ⇒ alokim; alokim ⇒ faturë e paguar me shumë të njëjtë; Σ credit notes ≤ total).

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

PAYMENT_COLS_G3 = ["id", "enterprise_id", "account_id", "purpose", "invoice_id", "currency", "amount", "source",
                   "external_reference", "note", "created_by_id", "created_by_label", "created_at"]  # fmt: skip
PAYMENT_COLS_OLD = ["id", "enterprise_id", "account_id", "currency", "amount", "source", "external_reference", "note",
                    "created_by_id", "created_by_label", "created_at"]  # fmt: skip


def _payments_guard(cols: list[str]) -> None:
    cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)
    op.execute(
        "CREATE OR REPLACE FUNCTION central_payments_guard() RETURNS trigger AS $$ "
        f"BEGIN IF {cond} THEN RAISE EXCEPTION 'payments money fields are immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status THEN RAISE EXCEPTION 'payments status is final' USING ERRCODE = '55000'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        batch.create_unique_constraint(
            "uq_invoices_id_enterprise_currency", ["id", "enterprise_id", "currency"]
        )
    with op.batch_alter_table("payments") as batch:
        batch.add_column(
            sa.Column("purpose", sa.String(length=8), server_default="credit", nullable=False)
        )
        batch.add_column(sa.Column("invoice_id", sa.Uuid(), nullable=True))
        batch.alter_column("account_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_foreign_key(
            "fk_payments_invoice_scope",
            "invoices",
            ["invoice_id", "enterprise_id", "currency"],
            ["id", "enterprise_id", "currency"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_payments_allocation_ref", ["id", "invoice_id", "currency", "amount"]
        )
        batch.create_check_constraint(
            op.f("ck_payments_purpose"), "purpose in ('credit', 'invoice')"
        )
        batch.create_check_constraint(
            op.f("ck_payments_purpose_shape"),
            "(purpose = 'credit' AND account_id IS NOT NULL AND invoice_id IS NULL) OR "
            "(purpose = 'invoice' AND account_id IS NULL AND invoice_id IS NOT NULL)",
        )
        batch.create_index("ix_payments_invoice_status", ["invoice_id", "status"], unique=False)
    op.create_table(
        "credit_note_sequence",
        sa.Column("year", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("last_number >= 0", name=op.f("ck_credit_note_sequence_non_negative")),
        sa.PrimaryKeyConstraint("year", name=op.f("pk_credit_note_sequence")),
    )
    op.create_table(
        "invoice_payment_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("allocated_by_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "amount > 0", name=op.f("ck_invoice_payment_allocations_amount_positive")
        ),
        sa.ForeignKeyConstraint(
            ["allocated_by_id"],
            ["users.id"],
            name=op.f("fk_invoice_payment_allocations_allocated_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id", "enterprise_id", "currency"],
            ["invoices.id", "invoices.enterprise_id", "invoices.currency"],
            name="fk_allocations_invoice_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id", "invoice_id", "currency", "amount"],
            ["payments.id", "payments.invoice_id", "payments.currency", "payments.amount"],
            name="fk_allocations_payment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_payment_allocations")),
        sa.UniqueConstraint("invoice_id", name="uq_allocations_invoice"),
        sa.UniqueConstraint("payment_id", name="uq_allocations_payment"),
    )
    op.create_table(
        "credit_notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.String(length=24), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.JSON(), nullable=False),
        sa.Column("bill_to", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name=op.f("ck_credit_notes_amount_positive")),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_credit_notes_currency"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_credit_notes_reason_required")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_credit_notes_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id", "enterprise_id", "currency"],
            ["invoices.id", "invoices.enterprise_id", "invoices.currency"],
            name="fk_credit_notes_invoice_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_notes")),
        sa.UniqueConstraint("invoice_id", "idempotency_key", name="uq_credit_notes_idempotency"),
        sa.UniqueConstraint("number", name=op.f("uq_credit_notes_number")),
    )
    op.create_index("ix_credit_notes_invoice", "credit_notes", ["invoice_id"], unique=False)
    op.create_index(
        "ix_credit_notes_enterprise", "credit_notes", ["enterprise_id", "issued_at"], unique=False
    )
    if op.get_bind().dialect.name == "postgresql":
        _pg_up()


def _pg_up() -> None:
    _payments_guard(PAYMENT_COLS_G3)
    for t in ("invoice_payment_allocations", "credit_notes"):
        op.execute(
            f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION central_billing_forbid()"
        )
    op.execute(
        "CREATE TRIGGER trg_credit_note_sequence_no_delete BEFORE DELETE ON credit_note_sequence FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
    )
    op.execute(
        "CREATE TRIGGER trg_credit_note_sequence_no_truncate BEFORE TRUNCATE ON credit_note_sequence FOR EACH STATEMENT EXECUTE FUNCTION central_billing_forbid()"
    )
    op.execute(
        "CREATE TRIGGER trg_credit_note_sequence_guard BEFORE UPDATE ON credit_note_sequence FOR EACH ROW EXECUTE FUNCTION central_invoice_number_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_allocation_consistency() RETURNS trigger AS $$ "
        "DECLARE inv invoices%ROWTYPE; pay payments%ROWTYPE; BEGIN "
        "SELECT * INTO inv FROM invoices WHERE id = NEW.invoice_id; SELECT * INTO pay FROM payments WHERE id = NEW.payment_id; "
        "IF inv.status <> 'paid' OR inv.total <> NEW.amount OR inv.currency <> NEW.currency THEN "
        "RAISE EXCEPTION 'allocation requires a paid invoice with the same total and currency' USING ERRCODE = '23514'; END IF; "
        "IF pay.purpose <> 'invoice' OR pay.status <> 'approved' OR pay.invoice_id <> NEW.invoice_id THEN "
        "RAISE EXCEPTION 'allocation requires an approved invoice-purpose payment for the same invoice' USING ERRCODE = '23514'; END IF; "
        "RETURN NULL; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_allocations_consistency AFTER INSERT ON invoice_payment_allocations DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION central_allocation_consistency()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_invoice_paid_has_allocation() RETURNS trigger AS $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM invoice_payment_allocations a WHERE a.invoice_id = NEW.id AND a.amount = NEW.total AND a.currency = NEW.currency) THEN "
        "RAISE EXCEPTION 'invoice % cannot be paid without a matching allocation', NEW.number USING ERRCODE = '23514'; END IF; RETURN NULL; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_invoices_paid_allocation AFTER UPDATE ON invoices DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW WHEN (NEW.status = 'paid' AND OLD.status IS DISTINCT FROM 'paid') EXECUTE FUNCTION central_invoice_paid_has_allocation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_payment_approved_has_allocation() RETURNS trigger AS $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM invoice_payment_allocations a WHERE a.payment_id = NEW.id) THEN "
        "RAISE EXCEPTION 'an approved invoice payment requires an allocation' USING ERRCODE = '23514'; END IF; RETURN NULL; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_payments_invoice_allocation AFTER UPDATE ON payments DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW WHEN (NEW.purpose = 'invoice' AND NEW.status = 'approved' AND OLD.status IS DISTINCT FROM 'approved') "
        "EXECUTE FUNCTION central_payment_approved_has_allocation()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_credit_note_consistency() RETURNS trigger AS $$ "
        "DECLARE inv invoices%ROWTYPE; s numeric; BEGIN "
        "SELECT * INTO inv FROM invoices WHERE id = NEW.invoice_id FOR UPDATE; "
        "IF inv.status <> 'paid' THEN RAISE EXCEPTION 'credit notes apply only to paid invoices' USING ERRCODE = '23514'; END IF; "
        "SELECT coalesce(sum(amount), 0) INTO s FROM credit_notes WHERE invoice_id = NEW.invoice_id; "
        "IF s > inv.total THEN RAISE EXCEPTION 'credit notes exceed the invoice total' USING ERRCODE = '23514'; END IF; RETURN NULL; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_credit_notes_consistency AFTER INSERT ON credit_notes DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION central_credit_note_consistency()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_credit_notes_consistency ON credit_notes",
            "DROP FUNCTION IF EXISTS central_credit_note_consistency()",
            "DROP TRIGGER IF EXISTS trg_payments_invoice_allocation ON payments",
            "DROP FUNCTION IF EXISTS central_payment_approved_has_allocation()",
            "DROP TRIGGER IF EXISTS trg_invoices_paid_allocation ON invoices",
            "DROP FUNCTION IF EXISTS central_invoice_paid_has_allocation()",
            "DROP TRIGGER IF EXISTS trg_allocations_consistency ON invoice_payment_allocations",
            "DROP FUNCTION IF EXISTS central_allocation_consistency()",
            "DROP TRIGGER IF EXISTS trg_credit_note_sequence_guard ON credit_note_sequence",
            "DROP TRIGGER IF EXISTS trg_credit_note_sequence_no_truncate ON credit_note_sequence",
            "DROP TRIGGER IF EXISTS trg_credit_note_sequence_no_delete ON credit_note_sequence",
            "DROP TRIGGER IF EXISTS trg_credit_notes_no_truncate ON credit_notes",
            "DROP TRIGGER IF EXISTS trg_credit_notes_immutable ON credit_notes",
            "DROP TRIGGER IF EXISTS trg_invoice_payment_allocations_no_truncate ON invoice_payment_allocations",
            "DROP TRIGGER IF EXISTS trg_invoice_payment_allocations_immutable ON invoice_payment_allocations",
        ):
            op.execute(stmt)
        _payments_guard(PAYMENT_COLS_OLD)
    op.drop_index("ix_credit_notes_enterprise", table_name="credit_notes")
    op.drop_index("ix_credit_notes_invoice", table_name="credit_notes")
    op.drop_table("credit_notes")
    op.drop_table("invoice_payment_allocations")
    op.drop_table("credit_note_sequence")
    with op.batch_alter_table("payments") as batch:
        batch.drop_index("ix_payments_invoice_status")
        batch.drop_constraint(op.f("ck_payments_purpose_shape"), type_="check")
        batch.drop_constraint(op.f("ck_payments_purpose"), type_="check")
        batch.drop_constraint("uq_payments_allocation_ref", type_="unique")
        batch.drop_constraint("fk_payments_invoice_scope", type_="foreignkey")
        batch.alter_column("account_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("invoice_id")
        batch.drop_column("purpose")
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint("uq_invoices_id_enterprise_currency", type_="unique")
