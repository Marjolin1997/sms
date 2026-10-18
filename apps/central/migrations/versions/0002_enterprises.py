"""Central: regjistri i Enterprise-ve (M4-b). Vetëm tabela `enterprises`.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enterprises",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('active', 'suspended')", name=op.f("ck_enterprises_status")),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_enterprises_name_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_enterprises")),
    )


def downgrade() -> None:
    op.drop_table("enterprises")
