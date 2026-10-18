"""M7-g: `sms_entitlements.rate_limit_per_min` (nullable, ADITIVE)

Kufi/min i assignment-it nga Central (SMS: mesazhe, Email: emaile); NULL = default lokal. Asgjë
nuk e lexon kur `SMS_CP_SYNC_MODE` ≠ enforce. Fushat legacy të AccountPlan mbeten të paprekura.

Revision ID: 0021
Revises: 0020
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sms_entitlements", sa.Column("rate_limit_per_min", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sms_entitlements") as b:
        b.drop_column("rate_limit_per_min")
