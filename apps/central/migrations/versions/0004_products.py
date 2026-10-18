"""Central: katalogu i produkteve (M5-a). Vetëm tabela `products`.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "code = lower(trim(code)) and length(code) >= 2",
            name=op.f("ck_products_code_canonical"),
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_products_name_not_blank")),
        sa.CheckConstraint("channel in ('sms', 'email')", name=op.f("ck_products_channel")),
        sa.CheckConstraint("status in ('active', 'retired')", name=op.f("ck_products_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("code", name=op.f("uq_products_code")),
    )


def downgrade() -> None:
    op.drop_table("products")
