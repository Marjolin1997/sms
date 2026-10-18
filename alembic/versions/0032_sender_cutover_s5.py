"""Enterprise: çështjet e bootstrap-it dhe prova e cutover-it (M10-S5). Aditiv; asnjë tabelë ekzistuese nuk preket.

- `sms_sender_bootstrap_issues`: çështje bllokuese me zgjidhje të vetme (NULL → vlerë), asnjë fshirje; një e hapur për (sender, kategori).
- `sms_sender_cutover_evidence`: prova append-only (hash UNIQUE) për cutover/post-cutover/rollback.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
CATS = ("identity_conflict", "global_key_conflict", "policy_denied", "invalid_legacy_identity", "missing_enterprise_mapping", "local_approved_central_pending", "local_approved_central_rejected", "local_approved_central_revoked", "missing_in_central")  # fmt: skip


def upgrade() -> None:
    op.create_table(
        "sms_sender_bootstrap_issues",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=True),
        sa.Column("sender_id", PK, nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("identity_hash", sa.String(length=16), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(length=24), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("evidence_ref", sa.String(length=128), nullable=True),
        sa.Column("report_hash", sa.String(length=64), nullable=True),
        sa.CheckConstraint("category in ('" + "', '".join(CATS) + "')", name=op.f("ck_sms_sender_bootstrap_issues_category")),
        sa.CheckConstraint("resolution IS NULL OR resolution in ('accepted_not_migrated', 'sender_deactivated', 'corrected')", name=op.f("ck_sms_sender_bootstrap_issues_resolution")),
        sa.CheckConstraint("(resolved_at IS NULL) = (resolution IS NULL)", name=op.f("ck_sms_sender_bootstrap_issues_resolved_consistency")),
        sa.CheckConstraint("resolution IS NULL OR (resolved_by IS NOT NULL AND length(trim(resolved_by)) > 0)", name=op.f("ck_sms_sender_bootstrap_issues_resolved_by")),
        sa.ForeignKeyConstraint(["sender_id"], ["sms_sender_ids.id"], name=op.f("fk_sms_sender_bootstrap_issues_sender_id_sms_sender_ids")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_bootstrap_issues")),
    )  # fmt: skip
    op.create_index(
        "ix_sms_sender_bootstrap_issues_sender",
        "sms_sender_bootstrap_issues",
        ["sender_id", "category"],
    )
    op.create_index(
        "uq_sms_sender_bootstrap_issues_open", "sms_sender_bootstrap_issues", ["sender_id", "category"], unique=True,
        sqlite_where=sa.text("resolved_at IS NULL"), postgresql_where=sa.text("resolved_at IS NULL"),
    )  # fmt: skip
    op.create_table(
        "sms_sender_cutover_evidence",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("authority_version", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=16), nullable=False),
        sa.Column("code_revision", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("bootstrap_report_hash", sa.String(length=64), nullable=True),
        sa.Column("readiness_status", sa.String(length=8), nullable=False),
        sa.Column("readiness_hash", sa.String(length=64), nullable=False),
        sa.Column("canary_ref", sa.String(length=64), nullable=True),
        sa.Column("ref_hash", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind in ('pre_cutover', 'post_cutover', 'rollback_ack')", name=op.f("ck_sms_sender_cutover_evidence_kind")),
        sa.CheckConstraint("readiness_status in ('PASS', 'WARN', 'FAIL')", name=op.f("ck_sms_sender_cutover_evidence_readiness_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_cutover_evidence")),
        sa.UniqueConstraint("evidence_hash", name=op.f("uq_sms_sender_cutover_evidence_evidence_hash")),
    )  # fmt: skip
    op.create_index(
        "ix_sms_sender_cutover_evidence_kind", "sms_sender_cutover_evidence", ["kind", "created_at"]
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_cutover_forbid() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION '% rows are never rewritten or deleted', TG_TABLE_NAME USING ERRCODE = '55000'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE OR REPLACE FUNCTION sms_sender_issue_guard() RETURNS trigger AS $$ "
            "BEGIN IF NEW.enterprise_id IS DISTINCT FROM OLD.enterprise_id OR NEW.sender_id <> OLD.sender_id OR NEW.category <> OLD.category "
            "OR NEW.identity_hash <> OLD.identity_hash OR NEW.detected_at <> OLD.detected_at OR (OLD.resolution IS NOT NULL) "
            "THEN RAISE EXCEPTION 'bootstrap issue identity and final resolution are immutable' USING ERRCODE = '55000'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_cutover_evidence_immutable BEFORE UPDATE OR DELETE ON sms_sender_cutover_evidence FOR EACH ROW EXECUTE FUNCTION sms_sender_cutover_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_cutover_evidence_no_truncate BEFORE TRUNCATE ON sms_sender_cutover_evidence FOR EACH STATEMENT EXECUTE FUNCTION sms_sender_cutover_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_bootstrap_issues_guard BEFORE UPDATE ON sms_sender_bootstrap_issues FOR EACH ROW EXECUTE FUNCTION sms_sender_issue_guard()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_bootstrap_issues_no_delete BEFORE DELETE ON sms_sender_bootstrap_issues FOR EACH ROW EXECUTE FUNCTION sms_sender_cutover_forbid()"
        )
        op.execute(
            "CREATE TRIGGER trg_sms_sender_bootstrap_issues_no_truncate BEFORE TRUNCATE ON sms_sender_bootstrap_issues FOR EACH STATEMENT EXECUTE FUNCTION sms_sender_cutover_forbid()"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for stmt in (
            "DROP TRIGGER IF EXISTS trg_sms_sender_bootstrap_issues_no_truncate ON sms_sender_bootstrap_issues",
            "DROP TRIGGER IF EXISTS trg_sms_sender_bootstrap_issues_no_delete ON sms_sender_bootstrap_issues",
            "DROP TRIGGER IF EXISTS trg_sms_sender_bootstrap_issues_guard ON sms_sender_bootstrap_issues",
            "DROP TRIGGER IF EXISTS trg_sms_sender_cutover_evidence_no_truncate ON sms_sender_cutover_evidence",
            "DROP TRIGGER IF EXISTS trg_sms_sender_cutover_evidence_immutable ON sms_sender_cutover_evidence",
            "DROP FUNCTION IF EXISTS sms_sender_issue_guard()",
            "DROP FUNCTION IF EXISTS sms_sender_cutover_forbid()",
        ):
            op.execute(stmt)
    op.drop_index("ix_sms_sender_cutover_evidence_kind", table_name="sms_sender_cutover_evidence")
    op.drop_table("sms_sender_cutover_evidence")
    op.drop_index("uq_sms_sender_bootstrap_issues_open", table_name="sms_sender_bootstrap_issues")
    op.drop_index("ix_sms_sender_bootstrap_issues_sender", table_name="sms_sender_bootstrap_issues")
    op.drop_table("sms_sender_bootstrap_issues")
