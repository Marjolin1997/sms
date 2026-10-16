"""Central: verifikimi i kontaktit + outbox njoftimesh (M8-e). Aditiv.

`registration_requests`: `verified_at`, `verification_nonce`, `verification_expires_at` (tokeni s'ruhet:
derivohet me HMAC). `notification_outbox`: outbox i ngushtë për email-et e verifikimit.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("registration_requests") as batch:
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("verification_nonce", sa.String(length=32), nullable=True))
        batch.add_column(
            sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "verified_clears_nonce",
            "verified_at IS NULL OR (verification_nonce IS NULL AND verification_expires_at IS NULL)",
        )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("registration_id", sa.Uuid(), nullable=False),
        sa.Column("recipient", sa.String(length=254), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=48), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state in ('pending', 'sending', 'sent', 'failed', 'superseded')",
            name=op.f("ck_notification_outbox_state"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_outbox_attempts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["registration_id"],
            ["registration_requests.id"],
            name=op.f("fk_notification_outbox_registration_id_registration_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
    )
    op.create_index(
        "ix_notification_outbox_state_available", "notification_outbox", ["state", "available_at"]
    )
    op.create_index(
        "ix_notification_outbox_registration",
        "notification_outbox",
        ["registration_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_registration", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_state_available", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    with op.batch_alter_table("registration_requests") as batch:
        batch.drop_constraint("verified_clears_nonce", type_="check")
        batch.drop_column("verification_expires_at")
        batch.drop_column("verification_nonce")
        batch.drop_column("verified_at")
