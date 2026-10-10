"""Central: provë e bootstrap-it të senderave ekzistues (M10-S4). Aditiv: një tabelë; asnjë tabelë ekzistuese nuk preket.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sender_bootstrap_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bootstrap_version", sa.Integer(), nullable=False),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=True),
        sa.Column("source_revision", sa.String(length=64), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_count", sa.Integer(), nullable=False),
        sa.Column("sender_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status in ('running', 'completed', 'failed')",
            name=op.f("ck_sender_bootstrap_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sender_bootstrap_runs")),
    )
    op.create_index(
        "ix_sender_bootstrap_runs_artifact",
        "sender_bootstrap_runs",
        ["artifact_hash", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sender_bootstrap_runs_artifact", table_name="sender_bootstrap_runs")
    op.drop_table("sender_bootstrap_runs")
