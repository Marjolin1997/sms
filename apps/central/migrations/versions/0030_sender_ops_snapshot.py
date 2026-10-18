"""Central: snapshot-i operacional i fundit i sender-ave nga Enterprise (M11-D2). Aditiv: një tabelë; asnjë tabelë ekzistuese nuk preket.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_ops_snapshots",
        sa.Column("client_pk", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.String(length=24), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "schema_version in ('sender.ops.v1')",
            name=op.f("ck_sender_ops_snapshots_schema_version"),
        ),
        sa.ForeignKeyConstraint(
            ["client_pk"],
            ["service_clients.id"],
            name=op.f("fk_sender_ops_snapshots_client_pk_service_clients"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("client_pk", name=op.f("pk_sender_ops_snapshots")),
    )


def downgrade() -> None:
    op.drop_table("sender_ops_snapshots")
