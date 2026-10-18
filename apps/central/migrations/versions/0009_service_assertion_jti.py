"""Central: mbrojtje replay për client assertions (M7-c): `service_assertion_jti`.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_assertion_jti",
        sa.Column("client_pk", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_pk"],
            ["service_clients.id"],
            name=op.f("fk_service_assertion_jti_client_pk_service_clients"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_pk", "jti", name=op.f("pk_service_assertion_jti")),
    )
    op.create_index("ix_service_assertion_jti_expires_at", "service_assertion_jti", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_service_assertion_jti_expires_at", table_name="service_assertion_jti")
    op.drop_table("service_assertion_jti")
