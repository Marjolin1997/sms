"""Central: politika e shtetit për sender-a, regjistri global dhe historia e vendimeve (M10-S1). Aditiv.

Tabela të reja: `country_sender_policies` (revizione append-only), `sender_registry` (projeksioni i gjendjes; identiteti i ngurtë), `sender_decisions` (historia append-only).
PostgreSQL: trigger-a që refuzojnë UPDATE/DELETE/TRUNCATE mbi politikat dhe vendimet; regjistri s'fshihet kurrë dhe identiteti i tij (enterprise, ref, shtet, vlerë, hash) s'ndryshon.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

KINDS = "('alphanumeric', 'numeric')"
STATUSES = "('pending', 'approved', 'rejected', 'revoked')"
DECISIONS = "('requested', 'approved', 'rejected', 'revoked', 'resubmitted')"
CATEGORIES = (
    "('request', 'manual', 'resubmit', 'policy_denied', 'policy_auto_approved', 'policy_revoked')"
)
SOURCES = "('admin', 'enterprise', 'import')"
IDENTITY = (
    "id",
    "enterprise_id",
    "external_ref",
    "country",
    "sender_kind",
    "display_value",
    "norm_value",
    "request_hash",
    "source",
    "created_at",
)


def upgrade() -> None:
    op.create_table(
        "country_sender_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sender_kind", sa.String(length=12), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(country) = 2 AND country = upper(country)",
            name=op.f("ck_country_sender_policies_country"),
        ),
        sa.CheckConstraint(f"sender_kind in {KINDS}", name=op.f("ck_country_sender_policies_kind")),
        sa.CheckConstraint(
            "revision >= 1", name=op.f("ck_country_sender_policies_revision_positive")
        ),
        sa.CheckConstraint(
            "allowed OR requires_approval",
            name=op.f("ck_country_sender_policies_denied_implies_review"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_country_sender_policies_created_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_country_sender_policies")),
        sa.UniqueConstraint(
            "country", "sender_kind", "revision", name="uq_country_sender_policies_revision"
        ),
        sa.UniqueConstraint(
            "country", "sender_kind", "effective_from", name="uq_country_sender_policies_effective"
        ),
    )
    op.create_index(
        "ix_country_sender_policies_lookup",
        "country_sender_policies",
        ["country", "sender_kind", "effective_from"],
    )
    op.create_table(
        "sender_registry",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sender_kind", sa.String(length=12), nullable=False),
        sa.Column("display_value", sa.String(length=16), nullable=False),
        sa.Column("norm_value", sa.String(length=16), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_key", sa.String(length=32), nullable=True),
        sa.Column("current_status", sa.String(length=12), nullable=False),
        sa.Column("current_decision_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(country) = 2 AND country = upper(country)",
            name=op.f("ck_sender_registry_country"),
        ),
        sa.CheckConstraint(f"sender_kind in {KINDS}", name=op.f("ck_sender_registry_kind")),
        sa.CheckConstraint(f"current_status in {STATUSES}", name=op.f("ck_sender_registry_status")),
        sa.CheckConstraint(f"source in {SOURCES}", name=op.f("ck_sender_registry_source")),
        sa.CheckConstraint(
            "(current_status = 'approved') = (approved_key IS NOT NULL)",
            name=op.f("ck_sender_registry_approved_key_consistency"),
        ),
        sa.CheckConstraint(
            "length(norm_value) >= 3 AND length(display_value) >= 3",
            name=op.f("ck_sender_registry_value_length"),
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_sender_registry_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_registry")),
        sa.UniqueConstraint("approved_key", name=op.f("uq_sender_registry_approved_key")),
        sa.UniqueConstraint(
            "enterprise_id", "external_ref", name="uq_sender_registry_external_ref"
        ),
        sa.UniqueConstraint(
            "enterprise_id", "country", "norm_value", name="uq_sender_registry_identity"
        ),
    )
    op.create_index(
        "ix_sender_registry_status", "sender_registry", ["current_status", "created_at"]
    )
    op.create_index(
        "ix_sender_registry_enterprise", "sender_registry", ["enterprise_id", "country"]
    )
    op.create_table(
        "sender_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False),
        sa.Column("from_status", sa.String(length=12), nullable=True),
        sa.Column("to_status", sa.String(length=12), nullable=False),
        sa.Column("category", sa.String(length=24), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("actor_label", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("evidence_ref", sa.String(length=128), nullable=True),
        sa.Column("policy_source", sa.String(length=8), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("policy_revision", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_sender_decisions_seq_positive")),
        sa.CheckConstraint(f"decision in {DECISIONS}", name=op.f("ck_sender_decisions_decision")),
        sa.CheckConstraint(f"category in {CATEGORIES}", name=op.f("ck_sender_decisions_category")),
        sa.CheckConstraint(f"source in {SOURCES}", name=op.f("ck_sender_decisions_source")),
        sa.CheckConstraint(
            "policy_source in ('explicit', 'default')",
            name=op.f("ck_sender_decisions_policy_source"),
        ),
        sa.CheckConstraint(
            "(decided_by_id IS NOT NULL) <> (actor_label IS NOT NULL)",
            name=op.f("ck_sender_decisions_actor"),
        ),
        sa.CheckConstraint(
            "(policy_source = 'explicit') = (policy_id IS NOT NULL AND policy_revision IS NOT NULL)",
            name=op.f("ck_sender_decisions_policy_provenance"),
        ),
        sa.CheckConstraint(
            "decision NOT IN ('rejected', 'revoked') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name=op.f("ck_sender_decisions_reason_required"),
        ),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["sender_registry.id"],
            name=op.f("fk_sender_decisions_registry_id_sender_registry"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_sender_decisions_decided_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            ["country_sender_policies.id"],
            name=op.f("fk_sender_decisions_policy_id_country_sender_policies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_decisions")),
        sa.UniqueConstraint("registry_id", "seq", name="uq_sender_decisions_seq"),
    )
    op.create_index(
        "ix_sender_decisions_registry", "sender_decisions", ["registry_id", "decided_at"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION central_sender_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        for t in ("country_sender_policies", "sender_decisions"):
            op.execute(
                f"CREATE TRIGGER trg_{t}_immutable BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION central_sender_forbid()"
            )
            op.execute(
                f"CREATE TRIGGER trg_{t}_no_truncate BEFORE TRUNCATE ON {t} FOR EACH STATEMENT EXECUTE FUNCTION central_sender_forbid()"
            )
        cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in IDENTITY)
        op.execute(
            "CREATE OR REPLACE FUNCTION central_sender_registry_guard() RETURNS trigger AS $$ "
            f"BEGIN IF {cond} THEN RAISE EXCEPTION 'sender registry identity is immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_registry_guard BEFORE UPDATE ON sender_registry FOR EACH ROW EXECUTE FUNCTION central_sender_registry_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_registry_no_delete BEFORE DELETE ON sender_registry FOR EACH ROW EXECUTE FUNCTION central_sender_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sender_registry_no_truncate BEFORE TRUNCATE ON sender_registry FOR EACH STATEMENT EXECUTE FUNCTION central_sender_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sender_registry_no_truncate ON sender_registry",
            "DROP TRIGGER IF EXISTS trg_sender_registry_no_delete ON sender_registry",
            "DROP TRIGGER IF EXISTS trg_sender_registry_guard ON sender_registry",
            "DROP FUNCTION IF EXISTS central_sender_registry_guard()",
            "DROP TRIGGER IF EXISTS trg_sender_decisions_no_truncate ON sender_decisions",
            "DROP TRIGGER IF EXISTS trg_sender_decisions_immutable ON sender_decisions",
            "DROP TRIGGER IF EXISTS trg_country_sender_policies_no_truncate ON country_sender_policies",
            "DROP TRIGGER IF EXISTS trg_country_sender_policies_immutable ON country_sender_policies",
            "DROP FUNCTION IF EXISTS central_sender_forbid()",
        ):
            op.execute(stmt)
    op.drop_index("ix_sender_decisions_registry", table_name="sender_decisions")
    op.drop_table("sender_decisions")
    op.drop_index("ix_sender_registry_enterprise", table_name="sender_registry")
    op.drop_index("ix_sender_registry_status", table_name="sender_registry")
    op.drop_table("sender_registry")
    op.drop_index("ix_country_sender_policies_lookup", table_name="country_sender_policies")
    op.drop_table("country_sender_policies")
