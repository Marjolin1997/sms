"""Central: epoka e feed-it dhe `floor_seq` te `sync_sequence` (M7-c).

`epoch` gjenerohet NJË HERË këtu (jo nga procesi) dhe ndryshon vetëm me procedurë eksplicite.
`floor_seq` = kursori më i vogël i shërbyeshëm me histori të plotë (0 sot; pastrimi do ta rrisë).

Revision ID: 0010
Revises: 0009
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sync_sequence") as batch:
        batch.add_column(sa.Column("epoch", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("floor_seq", sa.BigInteger(), server_default="0", nullable=False)
        )
    table = sa.table(
        "sync_sequence", sa.column("id", sa.SmallInteger), sa.column("epoch", sa.Uuid())
    )
    op.execute(table.update().where(table.c.id == 1).values(epoch=uuid.uuid4()))
    with op.batch_alter_table("sync_sequence") as batch:
        batch.alter_column("epoch", existing_type=sa.Uuid(), nullable=False)
        batch.create_check_constraint(
            "ck_sync_sequence_floor_within_range", "floor_seq >= 0 and floor_seq <= last_seq"
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_sequence") as batch:
        batch.drop_constraint("ck_sync_sequence_floor_within_range", type_="check")
        batch.drop_column("floor_seq")
        batch.drop_column("epoch")
