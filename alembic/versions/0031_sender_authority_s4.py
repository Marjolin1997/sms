"""Enterprise: autoriteti i sender-ave (M10-S4). Aditiv.

- `sms_messages`: 4 kolona NULLABLE të provenancës së autoritetit (pa default, pa constraint ⇒ pa rishkrim/skanim të tabelës): burimi (`local|central`), ref i regjistrit Central, ref i vendimit Central, rishikimi cp.
- `sms_sender_authority_comparisons`: krahasimet shadow (append-only; trigger PG), pa vlera sender.
- `sms_sender_bootstrap_state`: singleton — prova e rakordimit të bootstrap-it.

Revision ID: 0031
Revises: 0030
"""

import sqlalchemy as sa

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CATEGORIES = (
    "match_allowed", "match_denied", "local_allow_central_deny", "local_deny_central_allow",
    "central_missing", "central_pending", "central_rejected", "central_revoked",
    "policy_mismatch", "sender_identity_mismatch", "projection_stale",
)  # fmt: skip


def upgrade() -> None:
    op.add_column(
        "sms_messages", sa.Column("sender_authority_source", sa.String(length=8), nullable=True)
    )
    op.add_column("sms_messages", sa.Column("sender_registry_ref", sa.Uuid(), nullable=True))
    op.add_column(
        "sms_messages", sa.Column("sender_central_decision_ref", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "sms_messages", sa.Column("sender_central_revision", sa.BigInteger(), nullable=True)
    )
    op.create_table(
        "sms_sender_authority_comparisons",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("ref", sa.String(length=64), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sender_kind", sa.String(length=12), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("local_allowed", sa.Boolean(), nullable=False),
        sa.Column("central_allowed", sa.Boolean(), nullable=False),
        sa.Column("central_reason", sa.String(length=24), nullable=False),
        sa.Column("local_sender_ref", PK, nullable=True),
        sa.Column("central_registry_ref", sa.Uuid(), nullable=True),
        sa.Column("central_policy_revision", sa.BigInteger(), nullable=True),
        sa.Column("central_cp_revision", sa.BigInteger(), nullable=True),
        sa.Column("identity_hash", sa.String(length=16), nullable=False),
        sa.Column("projection_stale", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category in ('" + "', '".join(CATEGORIES) + "')",
            name=op.f("ck_sms_sender_authority_comparisons_category"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_authority_comparisons")),
    )
    op.create_index(
        "ix_sms_sender_authority_cmp_cat",
        "sms_sender_authority_comparisons",
        ["category", "created_at"],
    )
    op.create_table(
        "sms_sender_bootstrap_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bootstrap_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_revision", sa.String(length=64), nullable=True),
        sa.Column("tenant_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sender_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unresolved_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_sms_sender_bootstrap_state_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_bootstrap_state")),
    )
    state = sa.table("sms_sender_bootstrap_state", sa.column("id", sa.Integer))
    op.bulk_insert(state, [{"id": 1}])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_authority_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_authority_cmp_immutable BEFORE UPDATE OR DELETE ON sms_sender_authority_comparisons FOR EACH ROW EXECUTE FUNCTION sms_sender_authority_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_authority_cmp_no_truncate BEFORE TRUNCATE ON sms_sender_authority_comparisons FOR EACH STATEMENT EXECUTE FUNCTION sms_sender_authority_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_bootstrap_no_delete BEFORE DELETE ON sms_sender_bootstrap_state FOR EACH ROW EXECUTE FUNCTION sms_sender_authority_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sms_sender_bootstrap_no_delete ON sms_sender_bootstrap_state",
            "DROP TRIGGER IF EXISTS trg_sms_sender_authority_cmp_no_truncate ON sms_sender_authority_comparisons",
            "DROP TRIGGER IF EXISTS trg_sms_sender_authority_cmp_immutable ON sms_sender_authority_comparisons",
            "DROP FUNCTION IF EXISTS sms_sender_authority_forbid()",
        ):
            op.execute(stmt)
    op.drop_table("sms_sender_bootstrap_state")
    op.drop_index("ix_sms_sender_authority_cmp_cat", table_name="sms_sender_authority_comparisons")
    op.drop_table("sms_sender_authority_comparisons")
    with op.batch_alter_table("sms_messages") as b:
        b.drop_column("sender_central_revision")
        b.drop_column("sender_central_decision_ref")
        b.drop_column("sender_registry_ref")
        b.drop_column("sender_authority_source")
