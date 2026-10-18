"""Central: importi i faturimit legacy, autoriteti i faturimit, baseline-i i përdorimit, krahasimet shadow (M9-g4). Aditiv.

Tabela të reja: `billing_import_batches/items/issues`, `billing_usage_baselines`, `billing_authority_state`, `billing_shadow_comparisons`.
Ndryshime aditive: `invoices.provenance` (central|legacy_import; `plan_version_id` bëhet nullable VETËM për legacy_import me CHECK), `invoice_lines.line_type` pranon `legacy`,
`billing_periods.usage_from_baseline_id`. PostgreSQL: trigger-a immutability për prova/baseline/krahasime/batch-e, guard i `billing_authority_state` (singleton, pa fshirje) dhe
guard-i i faturave zgjerohet me `provenance`. Asnjë fshirje ose rikthim i të dhënave legacy.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

INV_OLD = ("id", "number", "enterprise_id", "subscription_id", "period_index", "period_start", "period_end", "plan_version_id", "currency", "subtotal",
           "vat_rate", "tax", "total", "bill_to", "issuer", "issued_at", "due_at", "created_at")  # fmt: skip
NEW_TABLES = (
    "billing_import_batches",
    "billing_import_items",
    "billing_import_issues",
    "billing_usage_baselines",
    "billing_authority_state",
    "billing_shadow_comparisons",
)


def _invoices_guard(with_provenance: bool) -> None:
    cols = list(INV_OLD) + (["provenance"] if with_provenance else [])
    cond = " OR ".join(
        f"NEW.{c} IS DISTINCT FROM OLD.{c}"
        if c not in ("bill_to", "issuer")
        else f"NEW.{c}::text IS DISTINCT FROM OLD.{c}::text"
        for c in cols
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_invoices_guard() RETURNS trigger AS $$ "
        f"BEGIN IF {cond} THEN RAISE EXCEPTION 'invoice % is immutable', OLD.number USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'open' AND (NEW.status IS DISTINCT FROM OLD.status OR NEW.paid_at IS DISTINCT FROM OLD.paid_at "
        "OR NEW.voided_at IS DISTINCT FROM OLD.voided_at OR NEW.voided_by_id IS DISTINCT FROM OLD.voided_by_id "
        "OR NEW.voided_reason IS DISTINCT FROM OLD.voided_reason) THEN "
        "RAISE EXCEPTION 'invoice % status is final', OLD.number USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    op.create_table(
        "billing_import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("export_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attestation", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("applied_by_id", sa.Uuid(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["applied_by_id"],
            ["users.id"],
            name=op.f("fk_billing_import_batches_applied_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_import_batches")),
        sa.UniqueConstraint("export_id", name=op.f("uq_billing_import_batches_export_id")),
    )
    op.create_table(
        "billing_import_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(length=16), nullable=False),
        sa.Column("source_table", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("last_batch_id", sa.Uuid(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "source_system = 'enterprise'", name=op.f("ck_billing_import_items_source_system")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["billing_import_batches.id"],
            name=op.f("fk_billing_import_items_batch_id_billing_import_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_batch_id"],
            ["billing_import_batches.id"],
            name=op.f("fk_billing_import_items_last_batch_id_billing_import_batches"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_import_items")),
        sa.UniqueConstraint(
            "source_system", "source_table", "source_id", name="uq_billing_import_items_source"
        ),
    )
    op.create_index(
        "ix_billing_import_items_target",
        "billing_import_items",
        ["target_type", "target_id"],
        unique=False,
    )
    op.create_table(
        "billing_import_issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_table", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", sa.Uuid(), nullable=True),
        sa.Column("resolution", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "classification in ('conflict', 'invalid', 'unsupported', 'requires_manual_review')",
            name=op.f("ck_billing_import_issues_classification"),
        ),
        sa.CheckConstraint(
            "(resolved_at IS NULL AND resolved_by_id IS NULL AND resolution IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by_id IS NOT NULL AND resolution IS NOT NULL AND length(trim(resolution)) > 0)",
            name=op.f("ck_billing_import_issues_resolution_consistency"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["billing_import_batches.id"],
            name=op.f("fk_billing_import_issues_batch_id_billing_import_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["users.id"],
            name=op.f("fk_billing_import_issues_resolved_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_import_issues")),
        sa.UniqueConstraint(
            "batch_id", "source_table", "source_id", name="uq_billing_import_issues_row"
        ),
    )
    op.create_index(
        "ix_billing_import_issues_open", "billing_import_issues", ["resolved_at"], unique=False
    )
    op.create_table(
        "billing_usage_baselines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("boundary", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cumulative_count", sa.BigInteger(), nullable=False),
        sa.Column("watermark", sa.BigInteger(), nullable=False),
        sa.Column("capture_active_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "cumulative_count >= 0 AND watermark >= cumulative_count",
            name=op.f("ck_billing_usage_baselines_counts"),
        ),
        sa.CheckConstraint(
            "capture_active_since <= boundary",
            name=op.f("ck_billing_usage_baselines_capture_covers_boundary"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_billing_usage_baselines_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_usage_baselines_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_billing_usage_baselines_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["billing_import_batches.id"],
            name=op.f("fk_billing_usage_baselines_source_batch_id_billing_import_batches"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_usage_baselines")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "boundary", name="uq_billing_usage_baselines_key"
        ),
    )
    op.create_table(
        "billing_authority_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("mode", sa.String(length=8), server_default="local", nullable=False),
        sa.Column("ack", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("changed_by_id", sa.Uuid(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_billing_authority_state_singleton")),
        sa.CheckConstraint(
            "mode in ('local', 'shadow', 'central')", name=op.f("ck_billing_authority_state_mode")
        ),
        sa.ForeignKeyConstraint(
            ["changed_by_id"],
            ["users.id"],
            name=op.f("fk_billing_authority_state_changed_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_authority_state")),
    )
    op.create_table(
        "billing_shadow_comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("period_index", sa.Integer(), nullable=False),
        sa.Column("legacy_invoice_id", sa.Uuid(), nullable=True),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("central", sa.JSON(), nullable=False),
        sa.Column("legacy", sa.JSON(), nullable=False),
        sa.Column("comparison_hash", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category in ('exact', 'amount_mismatch', 'usage_mismatch', 'period_mismatch', 'plan_mismatch', 'pricing_mismatch', "
            "'currency_mismatch', 'tax_mismatch', 'legacy_only', 'central_only', 'insufficient_usage')",
            name=op.f("ck_billing_shadow_comparisons_category"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_billing_shadow_comparisons_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_invoice_id"],
            ["invoices.id"],
            name=op.f("fk_billing_shadow_comparisons_legacy_invoice_id_invoices"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["billing_subscriptions.id"],
            name=op.f("fk_billing_shadow_comparisons_subscription_id_billing_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_shadow_comparisons")),
    )
    op.create_index(
        "ix_billing_shadow_sub_period",
        "billing_shadow_comparisons",
        ["subscription_id", "period_index", "computed_at"],
        unique=False,
    )
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(
            sa.Column("provenance", sa.String(length=16), server_default="central", nullable=False)
        )
        batch.alter_column("plan_version_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            op.f("ck_invoices_provenance"), "provenance in ('central', 'legacy_import')"
        )
        batch.create_check_constraint(
            op.f("ck_invoices_plan_version_required"),
            "provenance = 'legacy_import' OR plan_version_id IS NOT NULL",
        )
    with op.batch_alter_table("invoice_lines") as batch:
        batch.drop_constraint(op.f("ck_invoice_lines_line_type"), type_="check")
        batch.create_check_constraint(
            op.f("ck_invoice_lines_line_type"),
            "line_type in ('monthly_fee', 'email_overage', 'adjustment', 'legacy')",
        )
    with op.batch_alter_table("billing_periods") as batch:
        batch.add_column(
            sa.Column("provenance", sa.String(length=16), server_default="central", nullable=False)
        )
        batch.alter_column("plan_version_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(
            op.f("ck_billing_periods_provenance"), "provenance in ('central', 'legacy_import')"
        )
        batch.create_check_constraint(
            op.f("ck_billing_periods_plan_version_required"),
            "provenance = 'legacy_import' OR plan_version_id IS NOT NULL",
        )
        batch.add_column(sa.Column("usage_from_baseline_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_billing_periods_usage_from_baseline_id_billing_usage_baselines"),
            "billing_usage_baselines",
            ["usage_from_baseline_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if op.get_bind().dialect.name == "postgresql":
        _pg_up()


def _pg_up() -> None:
    _invoices_guard(True)
    for t in (
        "billing_import_batches",
        "billing_import_items",
        "billing_import_issues",
        "billing_usage_baselines",
        "billing_authority_state",
        "billing_shadow_comparisons",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION central_billing_forbid()"
        )
    for t in ("billing_import_batches", "billing_usage_baselines", "billing_shadow_comparisons"):
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_update BEFORE UPDATE ON {t} FOR EACH ROW EXECUTE FUNCTION central_billing_forbid()"
        )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_billing_import_items_guard() RETURNS trigger AS $$ BEGIN "
        "IF NEW.id IS DISTINCT FROM OLD.id OR NEW.source_system IS DISTINCT FROM OLD.source_system OR NEW.source_table IS DISTINCT FROM OLD.source_table "
        "OR NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.source_hash IS DISTINCT FROM OLD.source_hash OR NEW.target_type IS DISTINCT FROM OLD.target_type "
        "OR NEW.target_id IS DISTINCT FROM OLD.target_id OR NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.imported_at IS DISTINCT FROM OLD.imported_at THEN "
        "RAISE EXCEPTION 'import item evidence is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_billing_import_items_guard BEFORE UPDATE ON billing_import_items FOR EACH ROW EXECUTE FUNCTION central_billing_import_items_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_billing_import_issues_guard() RETURNS trigger AS $$ BEGIN "
        "IF NEW.id IS DISTINCT FROM OLD.id OR NEW.batch_id IS DISTINCT FROM OLD.batch_id OR NEW.source_table IS DISTINCT FROM OLD.source_table "
        "OR NEW.source_id IS DISTINCT FROM OLD.source_id OR NEW.classification IS DISTINCT FROM OLD.classification OR NEW.reason IS DISTINCT FROM OLD.reason "
        "OR NEW.created_at IS DISTINCT FROM OLD.created_at OR OLD.resolved_at IS NOT NULL THEN "
        "RAISE EXCEPTION 'import issues can only be resolved once' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_billing_import_issues_guard BEFORE UPDATE ON billing_import_issues FOR EACH ROW EXECUTE FUNCTION central_billing_import_issues_guard()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for t in ("billing_import_issues", "billing_import_items"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_guard ON {t}")
        op.execute("DROP FUNCTION IF EXISTS central_billing_import_issues_guard()")
        op.execute("DROP FUNCTION IF EXISTS central_billing_import_items_guard()")
        for t in (
            "billing_import_batches",
            "billing_usage_baselines",
            "billing_shadow_comparisons",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_update ON {t}")
        for t in NEW_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_truncate ON {t}")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_no_delete ON {t}")
        _invoices_guard(False)
    with op.batch_alter_table("billing_periods") as batch:
        batch.drop_constraint(
            op.f("fk_billing_periods_usage_from_baseline_id_billing_usage_baselines"),
            type_="foreignkey",
        )
        batch.drop_column("usage_from_baseline_id")
        batch.drop_constraint(op.f("ck_billing_periods_plan_version_required"), type_="check")
        batch.drop_constraint(op.f("ck_billing_periods_provenance"), type_="check")
        batch.alter_column("plan_version_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("provenance")
    with op.batch_alter_table("invoice_lines") as batch:
        batch.drop_constraint(op.f("ck_invoice_lines_line_type"), type_="check")
        batch.create_check_constraint(
            op.f("ck_invoice_lines_line_type"),
            "line_type in ('monthly_fee', 'email_overage', 'adjustment')",
        )
    with op.batch_alter_table("invoices") as batch:
        batch.drop_constraint(op.f("ck_invoices_plan_version_required"), type_="check")
        batch.drop_constraint(op.f("ck_invoices_provenance"), type_="check")
        batch.alter_column("plan_version_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("provenance")
    op.drop_index("ix_billing_shadow_sub_period", table_name="billing_shadow_comparisons")
    op.drop_table("billing_shadow_comparisons")
    op.drop_table("billing_authority_state")
    op.drop_table("billing_usage_baselines")
    op.drop_index("ix_billing_import_issues_open", table_name="billing_import_issues")
    op.drop_table("billing_import_issues")
    op.drop_index("ix_billing_import_items_target", table_name="billing_import_items")
    op.drop_table("billing_import_items")
    op.drop_table("billing_import_batches")
