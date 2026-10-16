"""Central: audit_log me aktor sistemi (M7-f).

`actor_kind` ('user' | 'system', default 'user'), `actor_id` bëhet NULLABLE, `actor_label` i ri.
CHECK: user ⇒ actor_id NOT NULL dhe actor_label NULL; system ⇒ actor_id NULL dhe actor_label
NOT NULL. Rreshtat ekzistues bëhen `user` (actor_id i pandryshuar). FK te `users.id` mbetet
`RESTRICT` për aktorët njerëz. Vetëm-shtim mbetet (ORM). Pa përdorues të rremë, pa tabelë të dytë.

Rikthimi: dështon qëllimisht nëse ekzistojnë rreshta `system` (s'kanë actor_id).

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

SEMANTICS = (
    "(actor_kind = 'user' AND actor_id IS NOT NULL AND actor_label IS NULL) OR "
    "(actor_kind = 'system' AND actor_id IS NULL AND actor_label IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.add_column(
            sa.Column("actor_kind", sa.String(length=8), server_default="user", nullable=False)
        )
        batch.add_column(sa.Column("actor_label", sa.String(length=64), nullable=True))
        batch.alter_column("actor_id", existing_type=sa.Uuid(), nullable=True)
        batch.create_check_constraint(op.f("ck_audit_log_actor_semantics"), SEMANTICS)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("select count(*) from audit_log where actor_kind = 'system'")).scalar():
        raise RuntimeError("cannot downgrade: audit_log contains system-actor rows")
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_constraint(op.f("ck_audit_log_actor_semantics"), type_="check")
        batch.alter_column("actor_id", existing_type=sa.Uuid(), nullable=False)
        batch.drop_column("actor_label")
        batch.drop_column("actor_kind")
