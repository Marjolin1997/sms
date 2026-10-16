"""Central: indekse për listën e faturave (M11-E2). Aditiv: dy indekse; asnjë tabelë/kolonë/kufizim nuk preket.

`ix_invoices_issued_at (issued_at, id)` (skanim prapa për ORDER BY issued_at DESC, id DESC) shërben renditjen e listës dhe filtrin e datave; `ix_invoices_number_prefix`
(PostgreSQL: varchar_pattern_ops) shërben `number LIKE 'INV-…%'` pavarësisht kolacionit. Matje: 300k fatura, lista pa filtër
87 ms → 0.05 ms, interval datash 13.5 ms → 0.07 ms, parafytyrë numri 26 ms → 0.1 ms (EXPLAIN ANALYZE, PostgreSQL 16).

Revision ID: 0031
Revises: 0030
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_invoices_issued_at", "invoices", ["issued_at", "id"])
    op.create_index(
        "ix_invoices_number_prefix",
        "invoices",
        ["number"],
        postgresql_ops={"number": "varchar_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_invoices_number_prefix", table_name="invoices")
    op.drop_index("ix_invoices_issued_at", table_name="invoices")
