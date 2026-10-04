"""Central: `service_clients.auto_grant_new_enterprises` (M8-c).

BOOLEAN NOT NULL DEFAULT false. Kur provisioning-u krijon një Enterprise TË RI, grantohet vetëm te
klientët `active` me flamurin true (rritet `auth_generation`). Pa grant retroaktiv; rreshtat
ekzistues mbeten false.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("service_clients") as batch:
        batch.add_column(
            sa.Column(
                "auto_grant_new_enterprises",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("service_clients") as batch:
        batch.drop_column("auto_grant_new_enterprises")
