"""M9-c: autoriteti i parave — kursor feed-i, baseline e pandryshueshme, grant-e të marra nga Central. Aditiv.

Asnjë ndryshim në tabela ekzistuese (EntryType merr vlerat GRANT/GRANT_REVERSAL si varg në kolonën
jo-native ekzistuese). PostgreSQL: triggers që e bëjnë baseline-in të pandryshueshëm (vetëm
status/superseded_at ndryshojnë) dhe refuzojnë DELETE mbi baseline/grant-e.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(20, 6)
_STATUSES = (
    "applied", "matched_to_existing_balance", "deferred_shadow", "baseline_mismatch", "unmapped",
    "reversed", "voided_before_apply", "reconciliation_required",
)  # fmt: skip
_FROZEN = (
    "baseline_ref", "wallet_id", "enterprise_id", "currency", "product_id", "available_at_cutover",
    "held_at_cutover", "gross_at_cutover", "ledger_max_id", "created_at", "created_by",
)  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "sms_money_cursor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=True),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_sms_money_cursor_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_money_cursor")),
    )
    op.create_table(
        "sms_money_baselines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("baseline_ref", sa.String(length=64), nullable=False),
        sa.Column(
            "wallet_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
        ),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("available_at_cutover", MONEY, nullable=False),
        sa.Column("held_at_cutover", MONEY, nullable=False),
        sa.Column("gross_at_cutover", MONEY, nullable=False),
        sa.Column("ledger_max_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["sms_wallets.id"],
            name=op.f("fk_sms_money_baselines_wallet_id_sms_wallets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_money_baselines")),
        sa.UniqueConstraint("baseline_ref", name=op.f("uq_sms_money_baselines_baseline_ref")),
        sa.CheckConstraint(
            "gross_at_cutover = available_at_cutover + held_at_cutover",
            name=op.f("ck_sms_money_baselines_gross"),
        ),
        sa.CheckConstraint(
            "available_at_cutover >= 0 AND held_at_cutover >= 0",
            name=op.f("ck_sms_money_baselines_non_negative"),
        ),
        sa.CheckConstraint("ledger_max_id >= 0", name=op.f("ck_sms_money_baselines_ledger_max_id")),
        sa.CheckConstraint(
            "status in ('active', 'superseded')", name=op.f("ck_sms_money_baselines_status")
        ),
        sa.CheckConstraint(
            "(status = 'active' AND superseded_at IS NULL) OR "
            "(status = 'superseded' AND superseded_at IS NOT NULL)",
            name=op.f("ck_sms_money_baselines_status_consistency"),
        ),
    )
    op.create_index("ix_sms_money_baselines_wallet_id", "sms_money_baselines", ["wallet_id"])
    op.create_index(
        "uq_sms_money_baselines_active_wallet", "sms_money_baselines", ["wallet_id"], unique=True,
        postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"),
    )  # fmt: skip
    op.create_table(
        "sms_money_grants",
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("baseline_ref", sa.String(length=64), nullable=True),
        sa.Column("wallet_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("issued_seq", sa.BigInteger(), nullable=False),
        sa.Column("issued_event_id", sa.Uuid(), nullable=False),
        sa.Column("issued_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("reversed_seq", sa.BigInteger(), nullable=True),
        sa.Column("reversed_event_id", sa.Uuid(), nullable=True),
        sa.Column(
            "ledger_entry_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column(
            "reversal_entry_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["sms_wallets.id"],
            name=op.f("fk_sms_money_grants_wallet_id_sms_wallets"),
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"],
            ["sms_ledger_entries.id"],
            name=op.f("fk_sms_money_grants_ledger_entry_id_sms_ledger_entries"),
        ),
        sa.ForeignKeyConstraint(
            ["reversal_entry_id"],
            ["sms_ledger_entries.id"],
            name=op.f("fk_sms_money_grants_reversal_entry_id_sms_ledger_entries"),
        ),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_sms_money_grants")),
        sa.UniqueConstraint("issued_event_id", name=op.f("uq_sms_money_grants_issued_event_id")),
        sa.UniqueConstraint(
            "reversed_event_id", name=op.f("uq_sms_money_grants_reversed_event_id")
        ),
        sa.UniqueConstraint("issued_seq", name="uq_sms_money_grants_issued_seq"),
        sa.CheckConstraint("amount > 0", name=op.f("ck_sms_money_grants_amount_positive")),
        sa.CheckConstraint(
            "purpose in ('standard', 'bootstrap')", name=op.f("ck_sms_money_grants_purpose")
        ),
        sa.CheckConstraint(
            "status in ('" + "', '".join(_STATUSES) + "')", name=op.f("ck_sms_money_grants_status")
        ),
    )
    op.create_index("ix_sms_money_grants_enterprise_id", "sms_money_grants", ["enterprise_id"])
    op.create_index("ix_sms_money_grants_status", "sms_money_grants", ["status"])
    op.create_index(
        "uq_sms_money_grants_matched_baseline", "sms_money_grants", ["baseline_ref"], unique=True,
        postgresql_where=sa.text("status = 'matched_to_existing_balance'"),
        sqlite_where=sa.text("status = 'matched_to_existing_balance'"),
    )  # fmt: skip
    if op.get_bind().dialect.name == "postgresql":
        cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _FROZEN)
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_money_baseline_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION 'baseline snapshot fields are immutable' "
            "USING ERRCODE = '55000'; END IF; "
            "IF OLD.status = 'superseded' AND NEW.status IS DISTINCT FROM OLD.status THEN "
            "RAISE EXCEPTION 'baseline status is final' USING ERRCODE = '55000'; END IF; "
            "RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_money_baselines_guard BEFORE UPDATE ON sms_money_baselines "
            "FOR EACH ROW EXECUTE FUNCTION sms_money_baseline_guard()"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_money_forbid_delete() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are never deleted', TG_TABLE_NAME "
            "USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        for t in ("sms_money_baselines", "sms_money_grants"):
            op.execute(
                f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} "
                "FOR EACH ROW EXECUTE FUNCTION sms_money_forbid_delete()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_sms_money_grants_no_delete ON sms_money_grants")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_sms_money_baselines_no_delete ON sms_money_baselines"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_sms_money_baselines_guard ON sms_money_baselines")
        op.execute("DROP FUNCTION IF EXISTS sms_money_forbid_delete()")
        op.execute("DROP FUNCTION IF EXISTS sms_money_baseline_guard()")
    op.drop_table("sms_money_grants")
    op.drop_table("sms_money_baselines")
    op.drop_table("sms_money_cursor")
