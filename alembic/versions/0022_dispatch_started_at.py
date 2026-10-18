"""M9-a: `dispatch_started_at` për sms_messages dhe sms_emails (aditiv, nullable).

Vendoset me COMMIT të veçantë para thirrjes së provider-it; përdoret vetëm nga sweeper-i i SENDING të
ngecur për të dalluar "provider-i definitivisht s'u thirr" (NULL) nga "mund të jetë thirrur" (vlerë).
Statusi `unknown` është varg në kolonën ekzistuese (Enum jo-native pa CHECK): s'ka ndryshim skeme.
Rreshtat historikë mbeten NULL dhe s'migrohen në UNKNOWN.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("sms_messages", "sms_emails"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(
                sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    for table in ("sms_emails", "sms_messages"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("dispatch_started_at")
