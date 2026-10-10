"""Central: politika e regjistrimit për produkt (M8-b) — vetëm `product_registration_policy`.

Aditiv. 1:1 me `products` (PK = product_id), pa fshirje. Pa `auto_grant` (revision tjetër, M8-c).

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_registration_policy",
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column(
            "self_registration_enabled", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("approval_mode", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "approval_mode in ('manual', 'automatic')",
            name=op.f("ck_product_registration_policy_approval_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_registration_policy_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_product_registration_policy")),
    )


def downgrade() -> None:
    op.drop_table("product_registration_policy")
