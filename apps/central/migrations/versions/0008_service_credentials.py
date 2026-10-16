"""Central: kredencialet e shërbimeve (M7-c): klientë, çelësa publikë Ed25519, objektivat.

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("auth_generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'disabled')", name=op.f("ck_service_clients_status")
        ),
        sa.CheckConstraint(
            "auth_generation >= 1", name=op.f("ck_service_clients_generation_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_clients")),
        sa.UniqueConstraint("client_id", name=op.f("uq_service_clients_client_id")),
    )
    op.create_table(
        "service_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_pk", sa.Uuid(), nullable=False),
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('active', 'disabled')", name=op.f("ck_service_keys_status")),
        sa.ForeignKeyConstraint(
            ["client_pk"],
            ["service_clients.id"],
            name=op.f("fk_service_keys_client_pk_service_clients"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_keys")),
        sa.UniqueConstraint("client_pk", "kid", name="uq_service_keys_client_pk_kid"),
    )
    op.create_table(
        "service_client_enterprises",
        sa.Column("client_pk", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_pk"],
            ["service_clients.id"],
            name=op.f("fk_service_client_enterprises_client_pk_service_clients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_id"],
            ["enterprises.id"],
            name=op.f("fk_service_client_enterprises_enterprise_id_enterprises"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "client_pk", "enterprise_id", name=op.f("pk_service_client_enterprises")
        ),
    )


def downgrade() -> None:
    op.drop_table("service_client_enterprises")
    op.drop_table("service_keys")
    op.drop_table("service_clients")
