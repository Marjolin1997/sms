"""M7-d: gjendja lokale e Control Plane (ADITIVE)

  * `sms_enterprises.cp_revision` BIGINT NOT NULL DEFAULT 0 (0 = asnjë gjendje autoritative e
    aplikuar); `short_name` zgjerohet 64 → 200 (emri i Central është ≤200; zgjerimi i VARCHAR në
    PostgreSQL është vetëm metadata, pa rishkrim tabele).
  * `sms_entitlements` (assignment Enterprise↔Product; pa `owner_ref`).
  * `sms_cp_cursor` singleton (epoch/generation NULL + last_seq 0 = kërkohet snapshot i plotë).

Asnjë tabelë tjetër (AccountPlan etj.) nuk preket; asgjë nuk lexon këto tabela ende.
Rikthimi: heq tabelat dhe kolonën (short_name rikthehet në 64: dështon nëse ka vlera më të gjata).

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sms_enterprises",
        sa.Column("cp_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    if op.get_bind().dialect.name == "postgresql":  # SQLite s'zbaton gjatësinë e VARCHAR
        op.alter_column(
            "sms_enterprises", "short_name", existing_type=sa.String(64), type_=sa.String(200),
            existing_nullable=True,
        )  # fmt: skip
    op.create_table(
        "sms_entitlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("product_code", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active','suspended','withdrawn')", name="ck_sms_entitlements_status"),
        sa.ForeignKeyConstraint(
            ["enterprise_id"], ["sms_enterprises.id"],
            name="fk_sms_entitlements_enterprise_id_sms_enterprises",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sms_entitlements"),
        sa.UniqueConstraint("assignment_id", name="uq_sms_entitlements_assignment_id"),
        sa.UniqueConstraint(
            "enterprise_id", "product_code", name="uq_sms_entitlements_enterprise_id"
        ),
    )  # fmt: skip
    op.create_index("ix_sms_entitlements_enterprise_id", "sms_entitlements", ["enterprise_id"])
    op.create_table(
        "sms_cp_cursor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=True),
        sa.Column("authorization_generation", sa.BigInteger(), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_sms_cp_cursor_singleton"),
        sa.PrimaryKeyConstraint("id", name="pk_sms_cp_cursor"),
    )
    op.execute("INSERT INTO sms_cp_cursor (id, last_seq) VALUES (1, 0)")


def downgrade() -> None:
    op.drop_table("sms_cp_cursor")
    op.drop_index("ix_sms_entitlements_enterprise_id", table_name="sms_entitlements")
    op.drop_table("sms_entitlements")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column(
            "sms_enterprises", "short_name", existing_type=sa.String(200), type_=sa.String(64),
            existing_nullable=True,
        )  # fmt: skip
    with op.batch_alter_table("sms_enterprises") as b:
        b.drop_column("cp_revision")
