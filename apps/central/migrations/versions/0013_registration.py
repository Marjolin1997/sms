"""Central: kërkesat e regjistrimit (M8-a) — vetëm `registration_requests` dhe `registration_products`.

Aditiv. Pa policy, pa provisioning, pa kolonë `auto_grant` (M8-b/c). `submission_key` është unik për
çdo kontakt (idempotencë e skopuar; NULL-et lejohen shumëfish). Vetëm hash i tokenit të aksesit.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contact_email", sa.String(length=254), nullable=False),
        sa.Column("contact_name", sa.String(length=120), nullable=True),
        sa.Column("enterprise_name", sa.String(length=200), nullable=False),
        sa.Column("submission_key", sa.String(length=64), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="submitted", nullable=False),
        sa.Column("decision_mode", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_label", sa.String(length=64), nullable=True),
        sa.Column("decision_reason", sa.String(length=500), nullable=True),
        sa.Column("provisioning_status", sa.String(length=16), nullable=True),
        sa.Column("provisioning_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("provisioning_error_code", sa.String(length=48), nullable=True),
        sa.Column("enterprise_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contact_email = lower(trim(contact_email))",
            name=op.f("ck_registration_requests_email_normalized"),
        ),
        sa.CheckConstraint(
            "length(trim(enterprise_name)) > 0",
            name=op.f("ck_registration_requests_name_not_blank"),
        ),
        sa.CheckConstraint(
            "length(access_token_hash) = 64", name=op.f("ck_registration_requests_token_hash_len")
        ),
        sa.CheckConstraint(
            "status in ('submitted', 'approved', 'rejected')",
            name=op.f("ck_registration_requests_status"),
        ),
        sa.CheckConstraint(
            "decision_mode IS NULL OR decision_mode in ('manual', 'automatic')",
            name=op.f("ck_registration_requests_decision_mode"),
        ),
        sa.CheckConstraint(
            "provisioning_status IS NULL OR provisioning_status in ('pending', 'provisioned', 'failed')",
            name=op.f("ck_registration_requests_provisioning_status"),
        ),
        sa.CheckConstraint(
            "(status = 'submitted' AND decided_at IS NULL AND decided_by_id IS NULL "
            "AND decided_by_label IS NULL AND decision_mode IS NULL) OR "
            "(status <> 'submitted' AND decided_at IS NOT NULL AND decision_mode IS NOT NULL AND "
            "((decided_by_id IS NOT NULL AND decided_by_label IS NULL) OR "
            "(decided_by_id IS NULL AND decided_by_label IS NOT NULL)))",
            name=op.f("ck_registration_requests_decision_consistency"),
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR "
            "(decision_reason IS NOT NULL AND length(trim(decision_reason)) > 0)",
            name=op.f("ck_registration_requests_reject_needs_reason"),
        ),
        sa.CheckConstraint(
            "(status = 'submitted' AND provisioning_status IS NULL) OR "
            "(status = 'approved' AND provisioning_status IS NOT NULL) OR "
            "(status = 'rejected' AND (provisioning_status IS NULL OR provisioning_status = 'failed'))",
            name=op.f("ck_registration_requests_provisioning_consistency"),
        ),
        sa.CheckConstraint(
            "provisioning_status IS NULL OR provisioning_status <> 'provisioned' "
            "OR enterprise_id IS NOT NULL",
            name=op.f("ck_registration_requests_provisioned_has_enterprise"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"],
            ["users.id"],
            name=op.f("fk_registration_requests_decided_by_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_registration_requests_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registration_requests")),
        sa.UniqueConstraint(
            "contact_email", "submission_key", name=op.f("uq_registration_requests_contact_email")
        ),
    )
    op.create_index(
        "ix_registration_requests_status_created", "registration_requests", ["status", "created_at"]
    )
    op.create_table(
        "registration_products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["registration_requests.id"],
            name=op.f("fk_registration_products_request_id_registration_requests"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_registration_products_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["enterprise_products.id"],
            name=op.f("fk_registration_products_assignment_id_enterprise_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_registration_products")),
        sa.UniqueConstraint(
            "request_id", "product_id", name=op.f("uq_registration_products_request_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("registration_products")
    op.drop_index("ix_registration_requests_status_created", table_name="registration_requests")
    op.drop_table("registration_requests")
