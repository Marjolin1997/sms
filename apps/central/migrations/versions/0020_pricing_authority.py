"""Central: autoriteti i çmimeve (M9-e). Aditiv.

price_books, price_versions (draft/active/retired, një draft per libër), price_rules (UNIQUE scope), price_assignments
(histori e pandryshueshme), pricing_sequence (epoch + revision për snapshot-in `cp.pricing.v1`). PostgreSQL: trigger-a që
e bëjnë rregullin e një versioni jo-draft të pandryshueshëm, mbrojnë fushat e versionit të aktivizuar dhe refuzojnë DELETE.

Revision ID: 0020
Revises: 0019
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

PRICE = sa.Numeric(20, 6)


def upgrade() -> None:
    op.create_table(
        "pricing_sequence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_pricing_sequence_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pricing_sequence")),
    )
    seq = sa.table(
        "pricing_sequence",
        sa.column("id", sa.Integer),
        sa.column("epoch", sa.Uuid),
        sa.column("revision", sa.BigInteger),
    )
    op.bulk_insert(seq, [{"id": 1, "epoch": uuid.uuid4(), "revision": 0}])
    op.create_table(
        "price_books",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_price_books_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_books")),
        sa.UniqueConstraint("code", name=op.f("uq_price_books_code")),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency = upper(currency)",
            name=op.f("ck_price_books_currency"),
        ),
    )
    op.create_table(
        "price_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_book_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("imported", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("activated_by_id", sa.Uuid(), nullable=True),
        sa.Column("retired_by_id", sa.Uuid(), nullable=True),
        sa.Column("retire_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["price_book_id"],
            ["price_books.id"],
            name=op.f("fk_price_versions_price_book_id_price_books"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_price_versions_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_id"],
            ["users.id"],
            name=op.f("fk_price_versions_activated_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retired_by_id"],
            ["users.id"],
            name=op.f("fk_price_versions_retired_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_versions")),
        sa.UniqueConstraint("price_book_id", "version", name="uq_price_versions_book_version"),
        sa.UniqueConstraint(
            "price_book_id", "effective_from", name="uq_price_versions_book_effective"
        ),
        sa.CheckConstraint(
            "status in ('draft', 'active', 'retired')", name=op.f("ck_price_versions_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_price_versions_version_positive")),
        sa.CheckConstraint(
            "(status = 'draft' AND effective_from IS NULL AND content_hash IS NULL AND activated_at IS NULL) OR "
            "(status <> 'draft' AND effective_from IS NOT NULL AND content_hash IS NOT NULL AND activated_at IS NOT NULL)",
            name=op.f("ck_price_versions_status_consistency"),
        ),
        sa.CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL AND retire_reason IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL AND retire_reason IS NULL)",
            name=op.f("ck_price_versions_retire_consistency"),
        ),
    )
    op.create_index(
        "uq_price_versions_one_draft", "price_versions", ["price_book_id"], unique=True,
        postgresql_where=sa.text("status = 'draft'"), sqlite_where=sa.text("status = 'draft'"),
    )  # fmt: skip
    op.create_table(
        "price_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=8), nullable=False),
        sa.Column("unit_price", PRICE, nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["price_versions.id"],
            name=op.f("fk_price_rules_version_id_price_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_rules")),
        sa.UniqueConstraint(
            "version_id", "channel", "prefix", "operator", name="uq_price_rules_scope"
        ),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_price_rules_price_non_negative")),
        sa.CheckConstraint("channel in ('sms', 'email')", name=op.f("ck_price_rules_channel")),
        sa.CheckConstraint(
            "(channel = 'sms' AND length(prefix) >= 1) OR (channel = 'email' AND prefix = '' AND operator = '')",
            name=op.f("ck_price_rules_scope"),
        ),
    )
    op.create_index(
        "ix_price_rules_version_prefix", "price_rules", ["version_id", "channel", "prefix"]
    )
    op.create_table(
        "price_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("price_book_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_price_assignments_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_price_assignments_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_book_id"],
            ["price_books.id"],
            name=op.f("fk_price_assignments_price_book_id_price_books"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_price_assignments_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_assignments")),
        sa.UniqueConstraint(
            "enterprise_id", "product_id", "effective_from", name="uq_price_assignments_scope"
        ),
    )
    op.create_index(
        "ix_price_assignments_enterprise",
        "price_assignments",
        ["enterprise_id", "product_id", "effective_from"],
    )
    if op.get_bind().dialect.name == "postgresql":
        _triggers()


def _triggers() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION central_pricing_forbid() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
    )
    for t in ("price_books", "price_versions", "price_assignments"):
        op.execute(
            f"CREATE TRIGGER trg_{t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION central_pricing_forbid()"
        )
    op.execute(
        "CREATE TRIGGER trg_price_assignments_no_update BEFORE UPDATE ON price_assignments FOR EACH ROW EXECUTE FUNCTION central_pricing_forbid()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_price_books_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.code IS DISTINCT FROM OLD.code OR NEW.name IS DISTINCT FROM OLD.name "
        "OR NEW.currency IS DISTINCT FROM OLD.currency THEN RAISE EXCEPTION 'price book is immutable' USING ERRCODE = '55000'; "
        "END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_price_books_guard BEFORE UPDATE ON price_books FOR EACH ROW EXECUTE FUNCTION central_price_books_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_price_versions_guard() RETURNS trigger AS $$ "
        "BEGIN IF NEW.id IS DISTINCT FROM OLD.id OR NEW.price_book_id IS DISTINCT FROM OLD.price_book_id "
        "OR NEW.version IS DISTINCT FROM OLD.version THEN "
        "RAISE EXCEPTION 'price version identity is immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'draft' AND (NEW.effective_from IS DISTINCT FROM OLD.effective_from "
        "OR NEW.content_hash IS DISTINCT FROM OLD.content_hash OR NEW.activated_at IS DISTINCT FROM OLD.activated_at "
        "OR NEW.activated_by_id IS DISTINCT FROM OLD.activated_by_id OR NEW.imported IS DISTINCT FROM OLD.imported) THEN "
        "RAISE EXCEPTION 'an activated price version is immutable' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'retired' AND NEW.status IS DISTINCT FROM OLD.status THEN "
        "RAISE EXCEPTION 'retired is final' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'active' AND NEW.status NOT IN ('active', 'retired') THEN "
        "RAISE EXCEPTION 'an active version can only be retired' USING ERRCODE = '55000'; END IF; "
        "IF OLD.status = 'draft' AND NEW.status NOT IN ('draft', 'active') THEN "
        "RAISE EXCEPTION 'a draft can only be activated' USING ERRCODE = '55000'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_price_versions_guard BEFORE UPDATE ON price_versions FOR EACH ROW EXECUTE FUNCTION central_price_versions_guard()"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION central_price_rules_guard() RETURNS trigger AS $$ "
        "DECLARE s text; vid uuid; BEGIN "
        "IF TG_OP = 'INSERT' THEN vid := NEW.version_id; ELSE vid := OLD.version_id; END IF; "
        "SELECT status INTO s FROM price_versions WHERE id = vid; "
        "IF s IS DISTINCT FROM 'draft' THEN RAISE EXCEPTION 'rules of a non-draft price version are immutable' USING ERRCODE = '55000'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_price_rules_guard BEFORE INSERT OR UPDATE OR DELETE ON price_rules "
        "FOR EACH ROW EXECUTE FUNCTION central_price_rules_guard()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_price_rules_guard ON price_rules",
            "DROP TRIGGER IF EXISTS trg_price_versions_guard ON price_versions",
            "DROP TRIGGER IF EXISTS trg_price_books_guard ON price_books",
            "DROP TRIGGER IF EXISTS trg_price_assignments_no_update ON price_assignments",
            "DROP TRIGGER IF EXISTS trg_price_assignments_no_delete ON price_assignments",
            "DROP TRIGGER IF EXISTS trg_price_versions_no_delete ON price_versions",
            "DROP TRIGGER IF EXISTS trg_price_books_no_delete ON price_books",
            "DROP FUNCTION IF EXISTS central_price_rules_guard()",
            "DROP FUNCTION IF EXISTS central_price_versions_guard()",
            "DROP FUNCTION IF EXISTS central_price_books_guard()",
            "DROP FUNCTION IF EXISTS central_pricing_forbid()",
        ):
            op.execute(stmt)
    op.drop_index("ix_price_assignments_enterprise", table_name="price_assignments")
    op.drop_table("price_assignments")
    op.drop_index("ix_price_rules_version_prefix", table_name="price_rules")
    op.drop_table("price_rules")
    op.drop_index("uq_price_versions_one_draft", table_name="price_versions")
    op.drop_table("price_versions")
    op.drop_table("price_books")
    op.drop_table("pricing_sequence")
