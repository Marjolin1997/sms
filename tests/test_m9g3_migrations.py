# ruff: noqa: F811
"""M9-g3 — migrimi Central 0024: aditiv, i kthyeshëm (up/down/up), metadata pa diferenca, trigger-at PG, rreshtat historikë mbeten `credit`."""

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401

NEW_TABLES = {"invoice_payment_allocations", "credit_notes", "credit_note_sequence"}
TOUCHED = NEW_TABLES | {"payments", "invoices"}


def _diff(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core.db import Base

    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
    )
    out = []
    for d in compare_metadata(ctx, Base.metadata):
        for it in d if isinstance(d, list) else [d]:
            if any(t in repr(it) for t in TOUCHED):
                out.append(repr(it))
    return out


def test_central_0024_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "0023")
    eng = create_engine(url)
    pay_cols = {c["name"]: c for c in inspect(eng).get_columns("payments")}
    assert (
        not NEW_TABLES & set(inspect(eng).get_table_names())
        and pay_cols["account_id"]["nullable"] is False
    )
    central_alembic(url, "upgrade", "head")
    tables = set(inspect(eng).get_table_names())
    assert NEW_TABLES <= tables
    cols = {c["name"]: c for c in inspect(eng).get_columns("payments")}
    assert (
        set(cols) - set(pay_cols) == {"purpose", "invoice_id"}
        and cols["account_id"]["nullable"] is True
    )
    assert (
        str(cols["purpose"]["default"]).strip("'\"()") in ("credit", "'credit'")
        or cols["purpose"]["default"] is not None
    )
    with eng.connect() as c:
        assert _diff(c) == []
        assert c.execute(text("select version_num from central_alembic_version")).scalar() == "0026"
    uq = {u["name"] for u in inspect(eng).get_unique_constraints("invoice_payment_allocations")}
    assert {"uq_allocations_payment", "uq_allocations_invoice"} <= uq
    central_alembic(url, "downgrade", "0023")
    assert not NEW_TABLES & set(inspect(eng).get_table_names())
    back = {c["name"]: c for c in inspect(eng).get_columns("payments")}
    assert set(back) == set(pay_cols) and back["account_id"]["nullable"] is False
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _diff(c) == []
    eng.dispose()


def test_existing_payments_default_to_credit_after_upgrade(make_db):
    """Rresht historik i krijuar para 0024 mbetet `credit` me llogarinë e vet (aditive, pa humbje)."""
    url = make_db("central")
    central_alembic(url, "upgrade", "0023")
    eng = create_engine(url)
    ids = {k: str(uuid.uuid4()) for k in ("e", "p", "a", "pay", "u")}
    if not url.startswith("postgresql"):
        ids = {k: v.replace("-", "") for k, v in ids.items()}
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO enterprises (id, name, status, created_at, updated_at) VALUES (:e, 'E', 'active', now_ts, now_ts)".replace(
                    "now_ts", "CURRENT_TIMESTAMP"
                )
            ),
            ids,
        )
        c.execute(
            text(
                "INSERT INTO products (id, code, name, channel, status, created_at, updated_at) VALUES (:p, 'sms', 'SMS', 'sms', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            ids,
        )
        c.execute(
            text(
                "INSERT INTO credit_accounts (id, enterprise_id, product_id, currency, status, created_at, updated_at) VALUES (:a, :e, :p, 'EUR', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            ids,
        )
        c.execute(
            text(
                "INSERT INTO payments (id, enterprise_id, account_id, currency, amount, source, status, created_by_label, created_at, updated_at) VALUES (:pay, :e, :a, 'EUR', 5, 'import', 'pending', 'system:import', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            ids,
        )
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        row = c.execute(
            text("SELECT purpose, invoice_id, account_id IS NOT NULL FROM payments")
        ).one()
        assert row[0] == "credit" and row[1] is None and bool(row[2])
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_0024_triggers_exist_and_are_removed_on_downgrade(make_db):
    url = make_db("central")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    q = text(
        "select tgname from pg_trigger where not tgisinternal and tgrelid::regclass::text = :t"
    )
    with eng.connect() as c:
        got = {
            t: {r[0] for r in c.execute(q, {"t": t})}
            for t in (
                "invoice_payment_allocations",
                "credit_notes",
                "credit_note_sequence",
                "invoices",
                "payments",
            )
        }
    assert {
        "trg_invoice_payment_allocations_immutable",
        "trg_invoice_payment_allocations_no_truncate",
        "trg_allocations_consistency",
    } <= got["invoice_payment_allocations"]
    assert {
        "trg_credit_notes_immutable",
        "trg_credit_notes_no_truncate",
        "trg_credit_notes_consistency",
    } <= got["credit_notes"]
    assert {
        "trg_credit_note_sequence_guard",
        "trg_credit_note_sequence_no_delete",
        "trg_credit_note_sequence_no_truncate",
    } <= got["credit_note_sequence"]
    assert (
        "trg_invoices_paid_allocation" in got["invoices"]
        and "trg_payments_invoice_allocation" in got["payments"]
    )
    central_alembic(url, "downgrade", "0023")
    with eng.connect() as c:
        left = c.execute(
            text(
                "select count(*) from pg_proc where proname in ('central_allocation_consistency','central_credit_note_consistency','central_invoice_paid_has_allocation','central_payment_approved_has_allocation')"
            )
        ).scalar()
        assert left == 0
        assert {r[0] for r in c.execute(q, {"t": "payments"})} >= {
            "trg_payments_guard",
            "trg_payments_no_delete",
        }
    central_alembic(url, "upgrade", "head")
    eng.dispose()
