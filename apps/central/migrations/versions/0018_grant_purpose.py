"""Central: grant me tip eksplicit (M9-c). Aditiv.

`credit_grants.purpose` (standard|bootstrap) dhe `baseline_ref` (identiteti i qëndrueshëm i baseline-it të
Enterprise; unik: një baseline ka një autorizim bootstrap). Fushat janë të ngrira (guard PostgreSQL).

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_OLD = [
    "id", "account_id", "enterprise_id", "product_id", "currency", "amount", "idempotency_key",
    "request_hash", "source_payment_id", "note", "created_by_id", "created_by_label", "created_at",
]  # fmt: skip


def _guard(cols: list[str]) -> None:
    cond = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in cols)
    op.execute(
        "CREATE OR REPLACE FUNCTION central_credit_grants_guard() RETURNS trigger AS $$ "
        f"BEGIN IF {cond} THEN RAISE EXCEPTION 'credit_grants money fields are immutable' "
        "USING ERRCODE = '55000'; END IF; "
        "IF OLD.status <> 'active' AND NEW.status IS DISTINCT FROM OLD.status THEN "
        "RAISE EXCEPTION 'credit_grants status is final' USING ERRCODE = '55000'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )


def upgrade() -> None:
    with op.batch_alter_table("credit_grants") as batch:
        batch.add_column(
            sa.Column("purpose", sa.String(length=16), nullable=False, server_default="standard")
        )
        batch.add_column(sa.Column("baseline_ref", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            op.f("ck_credit_grants_purpose"), "purpose in ('standard', 'bootstrap')"
        )
        batch.create_check_constraint(
            op.f("ck_credit_grants_purpose_baseline"),
            "(purpose = 'bootstrap' AND baseline_ref IS NOT NULL AND length(baseline_ref) = 64) OR "
            "(purpose = 'standard' AND baseline_ref IS NULL)",
        )
    op.create_index(
        "uq_credit_grants_baseline_ref", "credit_grants", ["baseline_ref"], unique=True,
        postgresql_where=sa.text("baseline_ref IS NOT NULL"),
        sqlite_where=sa.text("baseline_ref IS NOT NULL"),
    )  # fmt: skip
    if op.get_bind().dialect.name == "postgresql":
        _guard([*_OLD, "purpose", "baseline_ref"])


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _guard(_OLD)
    op.drop_index("uq_credit_grants_baseline_ref", table_name="credit_grants")
    with op.batch_alter_table("credit_grants") as batch:
        batch.drop_constraint(op.f("ck_credit_grants_purpose_baseline"), type_="check")
        batch.drop_constraint(op.f("ck_credit_grants_purpose"), type_="check")
        batch.drop_column("baseline_ref")
        batch.drop_column("purpose")
