"""Central: autoriteti tregtar i parave (M9-b). Aditiv; vetëm Central.

credit_accounts (një monedhë për enterprise/produkt), money_sequence (numërues transaksional),
commercial_ledger_entries (e pandryshueshme), payments, credit_grants, money_events (ditar i
pandryshueshëm). PostgreSQL: trigger-a që refuzojnë UPDATE/DELETE/TRUNCATE mbi ledger/events dhe
ndryshimin e fushave të ngrira të llogarisë/pagesës/grantit.

Revision ID: 0017
Revises: 0016
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 6)
CUR = "length(currency) = 3 AND currency = upper(currency)"


def _triggers() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION central_money_forbid_mutation() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000'; "
        "END; $$ LANGUAGE plpgsql"
    )
    for t in ("commercial_ledger_entries", "money_events"):
        op.execute(
            f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} "
            "FOR EACH ROW EXECUTE FUNCTION central_money_forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} "
            "FOR EACH STATEMENT EXECUTE FUNCTION central_money_forbid_mutation()"
        )
    guards = {
        "credit_accounts": ["id", "enterprise_id", "product_id", "currency", "created_at"],
        "payments": [
            "id",
            "enterprise_id",
            "account_id",
            "currency",
            "amount",
            "source",
            "external_reference",
            "note",
            "created_by_id",
            "created_by_label",
            "created_at",
        ],  # fmt: skip
        "credit_grants": [
            "id",
            "account_id",
            "enterprise_id",
            "product_id",
            "currency",
            "amount",
            "idempotency_key",
            "request_hash",
            "source_payment_id",
            "note",
            "created_by_id",
            "created_by_label",
            "created_at",
        ],  # fmt: skip
    }
    status_rules = {
        "payments": "OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status",
        "credit_grants": "OLD.status <> 'active' AND NEW.status IS DISTINCT FROM OLD.status",
    }
    for table, cols in guards.items():
        cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)
        extra = (
            f" IF {status_rules[table]} THEN RAISE EXCEPTION '{table} status is final' USING ERRCODE = '55000'; END IF;"
            if table in status_rules
            else ""
        )
        op.execute(
            f"CREATE OR REPLACE FUNCTION central_{table}_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION '{table} money fields are immutable' "
            f"USING ERRCODE = '55000'; END IF;{extra} RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_guard BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION central_{table}_guard()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION central_money_forbid_mutation()"
        )


def upgrade() -> None:
    op.create_table(
        "money_sequence",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_money_sequence_singleton")),
        sa.CheckConstraint("last_seq >= 0", name=op.f("ck_money_sequence_last_seq_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_money_sequence")),
    )
    seq = sa.table("money_sequence", sa.column("id", sa.SmallInteger), sa.column("epoch", sa.Uuid),
                   sa.column("last_seq", sa.BigInteger))  # fmt: skip
    op.bulk_insert(seq, [{"id": 1, "epoch": uuid.uuid4(), "last_seq": 0}])

    op.create_table(
        "credit_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_credit_accounts_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_credit_accounts_product_id_products"),
            ondelete="RESTRICT",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_accounts")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", name="uq_credit_accounts_enterprise_product"
        ),
        sa.UniqueConstraint("id", "currency", name="uq_credit_accounts_id_currency"),
        sa.UniqueConstraint(
            "id", "enterprise_id", "product_id", "currency", name="uq_credit_accounts_scope"
        ),
        sa.CheckConstraint(CUR, name=op.f("ck_credit_accounts_currency_format")),
        sa.CheckConstraint(
            "status in ('active', 'suspended')", name=op.f("ck_credit_accounts_status")
        ),
    )
    op.create_table(
        "commercial_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "currency"],
            ["credit_accounts.id", "credit_accounts.currency"],
            name="fk_commercial_ledger_entries_account_currency",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_commercial_ledger_entries_actor_user_id_users"),
            ondelete="RESTRICT",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_ledger_entries")),
        sa.UniqueConstraint("seq", name=op.f("uq_commercial_ledger_entries_seq")),
        sa.UniqueConstraint(
            "entry_type", "source_type", "source_id", name="uq_commercial_ledger_entries_source"
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_commercial_ledger_entries_amount_positive")),
        sa.CheckConstraint(
            "entry_type in ('payment_credit', 'manual_credit_adjustment', "
            "'manual_debit_adjustment', 'grant_issued', 'grant_reversal')",
            name=op.f("ck_commercial_ledger_entries_entry_type"),
        ),
        sa.CheckConstraint(
            "(actor_user_id IS NOT NULL AND actor_label IS NULL) OR "
            "(actor_user_id IS NULL AND actor_label IS NOT NULL)",
            name=op.f("ck_commercial_ledger_entries_actor_semantics"),
        ),
        sa.CheckConstraint(
            "entry_type NOT IN ('manual_credit_adjustment', 'manual_debit_adjustment', "
            "'grant_reversal') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name=op.f("ck_commercial_ledger_entries_reason_required"),
        ),
    )
    op.create_index(
        "ix_commercial_ledger_entries_account_seq",
        "commercial_ledger_entries",
        ["account_id", "seq"],
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("source", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_label", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by_id", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "enterprise_id", "currency"],
            ["credit_accounts.id", "credit_accounts.enterprise_id", "credit_accounts.currency"],
            name="fk_payments_account_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_payments_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
            name=op.f("fk_payments_approved_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by_id"],
            ["users.id"],
            name=op.f("fk_payments_rejected_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount_positive")),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected')", name=op.f("ck_payments_status")
        ),
        sa.CheckConstraint(
            "(created_by_id IS NOT NULL AND created_by_label IS NULL) OR "
            "(created_by_id IS NULL AND created_by_label IS NOT NULL)",
            name=op.f("ck_payments_creator_semantics"),
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND approved_at IS NULL AND approved_by_id IS NULL AND "
            "rejected_at IS NULL AND rejected_by_id IS NULL AND rejection_reason IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL AND approved_by_id IS NOT NULL AND "
            "rejected_at IS NULL AND rejected_by_id IS NULL AND rejection_reason IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL AND rejected_by_id IS NOT NULL AND "
            "approved_at IS NULL AND approved_by_id IS NULL AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0)",
            name=op.f("ck_payments_status_consistency"),
        ),
        sa.CheckConstraint(
            "approved_by_id IS NULL OR created_by_id IS NULL OR approved_by_id <> created_by_id",
            name=op.f("ck_payments_maker_checker"),
        ),
    )
    op.create_index(
        "uq_payments_source_external_reference",
        "payments",
        ["source", "external_reference"],
        unique=True,
        postgresql_where=sa.text("external_reference IS NOT NULL"),
        sqlite_where=sa.text("external_reference IS NOT NULL"),
    )
    op.create_index("ix_payments_account_status", "payments", ["account_id", "status"])
    op.create_table(
        "credit_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("source_payment_id", sa.Uuid(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_label", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_by_id", sa.Uuid(), nullable=True),
        sa.Column("reversal_reason", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id", "enterprise_id", "product_id", "currency"],
            [
                "credit_accounts.id",
                "credit_accounts.enterprise_id",
                "credit_accounts.product_id",
                "credit_accounts.currency",
            ],
            name="fk_credit_grants_account_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_payment_id"],
            ["payments.id"],
            name=op.f("fk_credit_grants_source_payment_id_payments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_credit_grants_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversed_by_id"],
            ["users.id"],
            name=op.f("fk_credit_grants_reversed_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_grants")),
        sa.UniqueConstraint("account_id", "idempotency_key", name="uq_credit_grants_idempotency"),
        sa.CheckConstraint("amount > 0", name=op.f("ck_credit_grants_amount_positive")),
        sa.CheckConstraint(
            "status in ('active', 'reversed')", name=op.f("ck_credit_grants_status")
        ),
        sa.CheckConstraint(
            "(created_by_id IS NOT NULL AND created_by_label IS NULL) OR "
            "(created_by_id IS NULL AND created_by_label IS NOT NULL)",
            name=op.f("ck_credit_grants_creator_semantics"),
        ),
        sa.CheckConstraint(
            "(status = 'active' AND reversed_at IS NULL AND reversed_by_id IS NULL AND "
            "reversal_reason IS NULL) OR "
            "(status = 'reversed' AND reversed_at IS NOT NULL AND reversed_by_id IS NOT NULL AND "
            "reversal_reason IS NOT NULL AND length(trim(reversal_reason)) > 0)",
            name=op.f("ck_credit_grants_status_consistency"),
        ),
    )
    op.create_index("ix_credit_grants_account_status", "credit_grants", ["account_id", "status"])
    op.create_table(
        "money_events",
        sa.Column("seq", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_money_events_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["credit_accounts.id"],
            name=op.f("fk_money_events_account_id_credit_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_money_events")),
        sa.UniqueConstraint("event_id", name=op.f("uq_money_events_event_id")),
        sa.UniqueConstraint("event_type", "entity_id", name="uq_money_events_entity"),
        sa.CheckConstraint("seq > 0", name=op.f("ck_money_events_seq_positive")),
        sa.CheckConstraint(
            "event_type in ('credit_grant.issued', 'credit_grant.reversed')",
            name=op.f("ck_money_events_event_type"),
        ),
    )
    op.create_index("ix_money_events_enterprise_seq", "money_events", ["enterprise_id", "seq"])
    _triggers()


def downgrade() -> None:
    op.drop_index("ix_money_events_enterprise_seq", table_name="money_events")
    op.drop_table("money_events")
    op.drop_index("ix_credit_grants_account_status", table_name="credit_grants")
    op.drop_table("credit_grants")
    op.drop_index("ix_payments_account_status", table_name="payments")
    op.drop_index("uq_payments_source_external_reference", table_name="payments")
    op.drop_table("payments")
    op.drop_index(
        "ix_commercial_ledger_entries_account_seq", table_name="commercial_ledger_entries"
    )
    op.drop_table("commercial_ledger_entries")
    op.drop_table("credit_accounts")
    op.drop_table("money_sequence")
    if op.get_bind().dialect.name == "postgresql":
        for fn in (
            "central_credit_accounts_guard",
            "central_payments_guard",
            "central_credit_grants_guard",
            "central_money_forbid_mutation",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {fn}()")
