"""Enterprise: `sms_audit_log.owner_ref` (M11-G). Aditiv: një kolonë nullable + një indeks.

Pa këtë kolonë nuk ka mënyrë të sigurt për të skopuar audit-in për një tenant (rreshti ka vetëm
actor/target). `audit()` e plotëson tani nga principal-i klient (ose `detail.owner`). Rreshtat e vjetër
mbeten NULL: NUK rindërtohen prapa (histori e panjohur) dhe klienti nuk i sheh kurrë (fail-closed).
Pa NOT NULL, pa rishkrim tabele.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sms_audit_log") as b:
        b.add_column(sa.Column("owner_ref", sa.String(64), nullable=True))
    op.create_index("ix_sms_audit_owner", "sms_audit_log", ["owner_ref", "id"])


def downgrade() -> None:
    op.drop_index("ix_sms_audit_owner", table_name="sms_audit_log")
    with op.batch_alter_table("sms_audit_log") as b:
        b.drop_column("owner_ref")
