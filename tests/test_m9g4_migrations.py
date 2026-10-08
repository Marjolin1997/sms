# ruff: noqa: F811
"""M9-g4 — migrimi Central 0025: aditiv, i kthyeshëm, metadata pa diferenca, trigger-at PG, rreshtat historikë mbeten të paprekur."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401

NEW = {
    "billing_import_batches",
    "billing_import_items",
    "billing_import_issues",
    "billing_usage_baselines",
    "billing_authority_state",
    "billing_shadow_comparisons",
}
TOUCHED = NEW | {"invoices", "invoice_lines", "billing_periods"}


def _diff(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core.db import Base

    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
    )
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if any(t in repr(i) for t in TOUCHED)
    ]


def test_central_0025_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "0024")
    eng = create_engine(url)
    inv_cols = {c["name"]: c for c in inspect(eng).get_columns("invoices")}
    per_cols = {c["name"] for c in inspect(eng).get_columns("billing_periods")}
    assert (
        not NEW & set(inspect(eng).get_table_names())
        and inv_cols["plan_version_id"]["nullable"] is False
    )
    central_alembic(url, "upgrade", "0025")
    assert NEW <= set(inspect(eng).get_table_names())
    inv2 = {c["name"]: c for c in inspect(eng).get_columns("invoices")}
    assert (
        set(inv2) - set(inv_cols) == {"provenance"} and inv2["plan_version_id"]["nullable"] is True
    )
    assert {c["name"] for c in inspect(eng).get_columns("billing_periods")} - per_cols == {
        "provenance",
        "usage_from_baseline_id",
    }
    with eng.connect() as c:
        assert _diff(c) == []
        assert c.execute(text("select version_num from central_alembic_version")).scalar() == "0025"
    central_alembic(url, "downgrade", "0024")
    assert not NEW & set(inspect(eng).get_table_names())
    assert {c["name"]: c["nullable"] for c in inspect(eng).get_columns("invoices")}[
        "plan_version_id"
    ] is False
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _diff(c) == []
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_0025_triggers_exist_and_are_removed_on_downgrade(make_db):
    url = make_db("central")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    q = text(
        "select tgname from pg_trigger where not tgisinternal and tgrelid::regclass::text = :t"
    )
    with eng.connect() as c:
        for t in NEW:
            names = {r[0] for r in c.execute(q, {"t": t})}
            assert f"trg_{t}_no_delete" in names and f"trg_{t}_no_truncate" in names, (t, names)
        assert "trg_billing_import_items_guard" in {
            r[0] for r in c.execute(q, {"t": "billing_import_items"})
        }
        assert "trg_billing_import_issues_guard" in {
            r[0] for r in c.execute(q, {"t": "billing_import_issues"})
        }
    central_alembic(url, "downgrade", "0024")
    with eng.connect() as c:
        assert (
            c.execute(
                text(
                    "select count(*) from pg_proc where proname in ('central_billing_import_items_guard','central_billing_import_issues_guard')"
                )
            ).scalar()
            == 0
        )
        src = c.execute(
            text("select prosrc from pg_proc where proname = 'central_invoices_guard'")
        ).scalar()
        assert "provenance" not in src
    central_alembic(url, "upgrade", "head")
    eng.dispose()
