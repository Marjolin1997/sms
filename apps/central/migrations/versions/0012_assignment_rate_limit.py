"""Central: `enterprise_products.rate_limit_per_min` (M7-g).

Një fushë për assignment: SMS = mesazhe/min, Email = emaile/min; NULL = default lokal i Enterprise.
CHECK: NULL ose 1..1_000_000. Pa fusha çmimi. Rreshtat ekzistues mbeten NULL.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

RANGE = "rate_limit_per_min IS NULL OR (rate_limit_per_min >= 1 AND rate_limit_per_min <= 1000000)"


def upgrade() -> None:
    with op.batch_alter_table("enterprise_products") as batch:
        batch.add_column(sa.Column("rate_limit_per_min", sa.Integer(), nullable=True))
        batch.create_check_constraint(op.f("ck_enterprise_products_rate_limit_range"), RANGE)


def downgrade() -> None:
    with op.batch_alter_table("enterprise_products") as batch:
        batch.drop_constraint(op.f("ck_enterprise_products_rate_limit_range"), type_="check")
        batch.drop_column("rate_limit_per_min")
