"""M9-e: autoriteti i çmimeve — cache e snapshot-it Central, krahasime shadow, snapshot çmimi në mesazh/linjë fature. Aditiv.

Tabela të reja: sms_pricing_state/snapshots/books/versions/rules/assignments/comparisons. `sms_messages` merr kolona të reja
(price_source, pricing_*_ref) dhe `rate_version_id`/`rate_id` bëhen NULLABLE (çmimi Central s'ka rresht ligjëruese); `sms_invoice_lines`
merr (pricing_source, pricing_version_ref). Asgjë s'fshihet/rishkruhet; tarifat ligjëruese mbeten deri te M13.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(20, 6)
_CLASS = ("match", "missing_rule", "currency_mismatch", "unit_price_mismatch", "total_mismatch", "precedence_mismatch",
          "version_missing", "segments_mismatch")  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "sms_pricing_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("epoch", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=True),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("first_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_sms_pricing_state_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_state")),
    )
    op.create_table(
        "sms_pricing_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_snapshots")),
        sa.UniqueConstraint("epoch", "revision", "authorization_generation", "snapshot_hash", name="uq_sms_pricing_snapshots_identity"),
    )
    op.create_table(
        "sms_pricing_books",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_books")),
    )
    op.create_table(
        "sms_pricing_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["book_id"], ["sms_pricing_books.id"], name=op.f("fk_sms_pricing_versions_book_id_sms_pricing_books")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_versions")),
        sa.UniqueConstraint("book_id", "version", name="uq_sms_pricing_versions_book_version"),
        sa.CheckConstraint("status in ('active', 'retired')", name=op.f("ck_sms_pricing_versions_status")),
    )
    op.create_index("ix_sms_pricing_versions_book_effective", "sms_pricing_versions", ["book_id", "effective_from"])
    op.create_table(
        "sms_pricing_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=8), nullable=False),
        sa.Column("unit_price", PRICE, nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["sms_pricing_versions.id"], name=op.f("fk_sms_pricing_rules_version_id_sms_pricing_versions")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_rules")),
        sa.UniqueConstraint("version_id", "channel", "prefix", "operator", name="uq_sms_pricing_rules_scope"),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_sms_pricing_rules_price_non_negative")),
        sa.CheckConstraint("channel in ('sms', 'email')", name=op.f("ck_sms_pricing_rules_channel")),
    )
    op.create_index("ix_sms_pricing_rules_lookup", "sms_pricing_rules", ["version_id", "channel", "prefix"])
    op.create_table(
        "sms_pricing_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("book_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["sms_pricing_snapshots.id"], name=op.f("fk_sms_pricing_assignments_snapshot_id_sms_pricing_snapshots")),
        sa.ForeignKeyConstraint(["book_id"], ["sms_pricing_books.id"], name=op.f("fk_sms_pricing_assignments_book_id_sms_pricing_books")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_assignments")),
        sa.UniqueConstraint("snapshot_id", "assignment_id", name="uq_sms_pricing_assignments_snapshot"),
    )
    op.create_index("ix_sms_pricing_assignments_lookup", "sms_pricing_assignments", ["snapshot_id", "enterprise_id", "product_id", "effective_from"])
    op.create_table(
        "sms_pricing_comparisons",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=24), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("legacy_currency", sa.String(length=3), nullable=True),
        sa.Column("legacy_unit_price", PRICE, nullable=True),
        sa.Column("legacy_segments", sa.Integer(), nullable=True),
        sa.Column("legacy_total", PRICE, nullable=True),
        sa.Column("central_currency", sa.String(length=3), nullable=True),
        sa.Column("central_unit_price", PRICE, nullable=True),
        sa.Column("central_segments", sa.Integer(), nullable=True),
        sa.Column("central_total", PRICE, nullable=True),
        sa.Column("central_version_id", sa.Uuid(), nullable=True),
        sa.Column("detail", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_pricing_comparisons")),
        sa.CheckConstraint("kind in ('sms', 'email')", name=op.f("ck_sms_pricing_comparisons_kind")),
    )
    op.create_index("ix_sms_pricing_comparisons_created", "sms_pricing_comparisons", ["created_at"])
    with op.batch_alter_table("sms_messages") as batch:
        batch.add_column(sa.Column("price_source", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("pricing_book_ref", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("pricing_version_ref", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("pricing_rule_ref", sa.Uuid(), nullable=True))
        batch.alter_column("rate_version_id", existing_type=sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True)
        batch.alter_column("rate_id", existing_type=sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True)
    with op.batch_alter_table("sms_invoice_lines") as batch:
        batch.add_column(sa.Column("pricing_source", sa.String(length=12), nullable=True))
        batch.add_column(sa.Column("pricing_version_ref", sa.Uuid(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        _triggers()


def _triggers() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_pricing_forbid() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    for t in ("sms_pricing_books", "sms_pricing_rules", "sms_pricing_assignments", "sms_pricing_snapshots", "sms_pricing_comparisons"):
        op.execute(f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION sms_pricing_forbid()")
    op.execute(f"CREATE TRIGGER trg_sms_pricing_versions_no_delete BEFORE DELETE ON sms_pricing_versions FOR EACH ROW EXECUTE FUNCTION sms_pricing_forbid()")
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_pricing_versions_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.book_id IS DISTINCT FROM OLD.book_id OR NEW.version IS DISTINCT FROM OLD.version "
        "OR NEW.effective_from IS DISTINCT FROM OLD.effective_from OR NEW.content_hash IS DISTINCT FROM OLD.content_hash "
        "OR NEW.rule_count IS DISTINCT FROM OLD.rule_count THEN RAISE EXCEPTION 'pricing version is immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM OLD.status THEN RAISE EXCEPTION 'retired is final' USING ERRCODE = '55000'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute("CREATE TRIGGER trg_sms_pricing_versions_guard BEFORE UPDATE ON sms_pricing_versions FOR EACH ROW EXECUTE FUNCTION sms_pricing_versions_guard()")
    op.execute(
        "CREATE OR REPLACE FUNCTION sms_message_price_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.currency IS DISTINCT FROM OLD.currency OR NEW.unit_price IS DISTINCT FROM OLD.unit_price "
        "OR NEW.total_price IS DISTINCT FROM OLD.total_price OR NEW.segments IS DISTINCT FROM OLD.segments "
        "OR NEW.encoding IS DISTINCT FROM OLD.encoding OR NEW.rate_version_id IS DISTINCT FROM OLD.rate_version_id "
        "OR NEW.rate_id IS DISTINCT FROM OLD.rate_id OR NEW.price_source IS DISTINCT FROM OLD.price_source "
        "OR NEW.pricing_book_ref IS DISTINCT FROM OLD.pricing_book_ref OR NEW.pricing_version_ref IS DISTINCT FROM OLD.pricing_version_ref "
        "OR NEW.pricing_rule_ref IS DISTINCT FROM OLD.pricing_rule_ref THEN "
        "RAISE EXCEPTION 'message price snapshot is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute("CREATE TRIGGER trg_sms_messages_price_guard BEFORE UPDATE ON sms_messages FOR EACH ROW EXECUTE FUNCTION sms_message_price_guard()")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sms_messages_price_guard ON sms_messages",
            "DROP FUNCTION IF EXISTS sms_message_price_guard()",
            "DROP TRIGGER IF EXISTS trg_sms_pricing_versions_guard ON sms_pricing_versions",
            "DROP TRIGGER IF EXISTS trg_sms_pricing_versions_no_delete ON sms_pricing_versions",
            "DROP FUNCTION IF EXISTS sms_pricing_versions_guard()",
        ):
            op.execute(stmt)
        for t in ("sms_pricing_books", "sms_pricing_rules", "sms_pricing_assignments", "sms_pricing_snapshots", "sms_pricing_comparisons"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{t}_immutable ON {t}")
        op.execute("DROP FUNCTION IF EXISTS sms_pricing_forbid()")
    with op.batch_alter_table("sms_invoice_lines") as batch:
        batch.drop_column("pricing_version_ref")
        batch.drop_column("pricing_source")
    with op.batch_alter_table("sms_messages") as batch:
        batch.drop_column("pricing_rule_ref")
        batch.drop_column("pricing_version_ref")
        batch.drop_column("pricing_book_ref")
        batch.drop_column("price_source")
        batch.alter_column("rate_id", existing_type=sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False)
        batch.alter_column("rate_version_id", existing_type=sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False)
    op.drop_index("ix_sms_pricing_comparisons_created", table_name="sms_pricing_comparisons")
    op.drop_table("sms_pricing_comparisons")
    op.drop_index("ix_sms_pricing_assignments_lookup", table_name="sms_pricing_assignments")
    op.drop_table("sms_pricing_assignments")
    op.drop_index("ix_sms_pricing_rules_lookup", table_name="sms_pricing_rules")
    op.drop_table("sms_pricing_rules")
    op.drop_index("ix_sms_pricing_versions_book_effective", table_name="sms_pricing_versions")
    op.drop_table("sms_pricing_versions")
    op.drop_table("sms_pricing_books")
    op.drop_table("sms_pricing_snapshots")
    op.drop_table("sms_pricing_state")
