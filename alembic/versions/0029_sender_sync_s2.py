"""Enterprise: projeksioni i sinkronizuar `cp.sender.v1` + kursori i tij (M10-S2). Aditiv; asnjë tabelë ekzistuese nuk preket (`sms_sender_ids` mbetet objekti lokal).

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "sms_synced_sender_policies",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sender_kind", sa.String(length=12), nullable=False),
        sa.Column("allowed", sa.Boolean(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cp_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "projection_state", sa.String(length=10), server_default="active", nullable=False
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sender_kind in ('alphanumeric', 'numeric')",
            name=op.f("ck_sms_synced_sender_policies_kind"),
        ),
        sa.CheckConstraint(
            "projection_state in ('active', 'withdrawn')",
            name=op.f("ck_sms_synced_sender_policies_state"),
        ),
        sa.CheckConstraint(
            "policy_revision >= 1", name=op.f("ck_sms_synced_sender_policies_revision_positive")
        ),
        sa.CheckConstraint(
            "allowed OR requires_approval",
            name=op.f("ck_sms_synced_sender_policies_denied_implies_review"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_synced_sender_policies")),
        sa.UniqueConstraint("country", "sender_kind", name="uq_sms_synced_sender_policies_scope"),
    )
    op.create_table(
        "sms_synced_sender_authorizations",
        sa.Column("id", PK, autoincrement=True, nullable=False),
        sa.Column("registry_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(length=64), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("sender_kind", sa.String(length=12), nullable=False),
        sa.Column("display_value", sa.String(length=16), nullable=False),
        sa.Column("norm_value", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("approved_key", sa.String(length=32), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=12), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_source", sa.String(length=8), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=True),
        sa.Column("policy_revision", sa.BigInteger(), nullable=True),
        sa.Column("cp_revision", sa.BigInteger(), nullable=False),
        sa.Column("cp_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "projection_state", sa.String(length=10), server_default="active", nullable=False
        ),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sender_kind in ('alphanumeric', 'numeric')",
            name=op.f("ck_sms_synced_sender_authorizations_kind"),
        ),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected', 'revoked')",
            name=op.f("ck_sms_synced_sender_authorizations_status"),
        ),
        sa.CheckConstraint(
            "projection_state in ('active', 'withdrawn')",
            name=op.f("ck_sms_synced_sender_authorizations_state"),
        ),
        sa.CheckConstraint(
            "policy_source in ('explicit', 'default')",
            name=op.f("ck_sms_synced_sender_authorizations_policy_source"),
        ),
        sa.CheckConstraint(
            "cp_revision >= 1", name=op.f("ck_sms_synced_sender_authorizations_revision_positive")
        ),
        sa.CheckConstraint(
            "approved_key IS NULL OR (status = 'approved' AND projection_state = 'active')",
            name=op.f("ck_sms_synced_sender_authorizations_approved_key_consistency"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_synced_sender_authorizations")),
        sa.UniqueConstraint(
            "registry_id", name=op.f("uq_sms_synced_sender_authorizations_registry_id")
        ),
        sa.UniqueConstraint(
            "approved_key", name=op.f("uq_sms_synced_sender_authorizations_approved_key")
        ),
    )
    op.create_index(
        "ix_sms_synced_sender_auth_lookup",
        "sms_synced_sender_authorizations",
        ["enterprise_id", "country", "norm_value"],
    )
    op.create_table(
        "sms_sender_sync_cursor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=True),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("latest_central_seq", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_seq", sa.BigInteger(), nullable=True),
        sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("gap_recoveries", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("drift_repairs", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_sms_sender_sync_cursor_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sms_sender_sync_cursor")),
    )
    op.execute(
        "INSERT INTO sms_sender_sync_cursor (id, last_seq, failure_count, gap_recoveries, drift_repairs) VALUES (1, 0, 0, 0, 0)"
    )


def downgrade() -> None:
    op.drop_table("sms_sender_sync_cursor")
    op.drop_index("ix_sms_synced_sender_auth_lookup", table_name="sms_synced_sender_authorizations")
    op.drop_table("sms_synced_sender_authorizations")
    op.drop_table("sms_synced_sender_policies")
