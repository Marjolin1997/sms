# ruff: noqa: F811
"""M9-g2 — migrimet aditive: Enterprise 0027 (prova + outbox) dhe Central 0023 (raportet + prova e deltës); up/down/up, metadata, trigger-at PG."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.test_central import IS_PG, central_alembic, enterprise_alembic, make_db  # noqa: F401

ENT_TABLES = {"sms_email_billable_events", "sms_billing_usage_reports"}


def _diff_for(conn, metadata, version_table, tables):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "version_table": version_table}
    )
    out = []
    for d in compare_metadata(ctx, metadata):
        items = d if isinstance(d, list) else [d]
        for it in items:
            blob = repr(it)
            if any(t in blob for t in tables):
                out.append(blob)
    return out


def test_enterprise_0027_is_additive_reversible_and_matches_metadata(make_db):
    from app.core.db import Base

    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0026")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert not ENT_TABLES & set(before)
    enterprise_alembic(url, "upgrade", "0027")
    after = inspect(eng).get_table_names()
    assert ENT_TABLES <= set(after)
    for t, cols in before.items():  # asnjë tabelë ekzistuese s'ndryshon
        assert {c["name"] for c in inspect(eng).get_columns(t)} == cols
    uq = inspect(eng).get_unique_constraints("sms_email_billable_events")
    assert any(u["column_names"] == ["email_id"] for u in uq)
    with eng.connect() as c:
        assert _diff_for(c, Base.metadata, "alembic_version", ENT_TABLES) == []
    enterprise_alembic(url, "downgrade", "0026")
    assert not ENT_TABLES & set(inspect(eng).get_table_names())
    enterprise_alembic(url, "upgrade", "0027")
    assert ENT_TABLES <= set(inspect(eng).get_table_names())
    eng.dispose()


def test_central_0023_is_additive_reversible_and_matches_metadata(make_db):
    from apps.central.core.db import Base

    url = make_db("central")
    central_alembic(url, "upgrade", "0022")
    eng = create_engine(url)
    cols_before = {c["name"] for c in inspect(eng).get_columns("billing_periods")}
    assert "billing_usage_reports" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    assert "billing_usage_reports" in inspect(eng).get_table_names()
    cols = {c["name"] for c in inspect(eng).get_columns("billing_periods")}
    assert cols - cols_before - {"usage_from_baseline_id", "provenance"} == {
        "usage_from_report_id",
        "usage_to_report_id",
    }  # M9-g4 shton usage_from_baseline_id
    with eng.connect() as c:
        assert (
            _diff_for(
                c,
                Base.metadata,
                "central_alembic_version",
                {"billing_usage_reports", "billing_periods"},
            )
            == []
        )
        assert c.execute(text("select version_num from central_alembic_version")).scalar() == "0026"
    central_alembic(url, "downgrade", "0022")
    assert "billing_usage_reports" not in inspect(eng).get_table_names()
    assert {c["name"] for c in inspect(eng).get_columns("billing_periods")} == cols_before
    central_alembic(url, "upgrade", "head")
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_exist_and_are_removed_on_downgrade(make_db):
    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    q = text(
        "select tgname from pg_trigger where not tgisinternal and tgrelid::regclass::text = :t"
    )
    with eng.connect() as c:
        assert {r[0] for r in c.execute(q, {"t": "sms_email_billable_events"})} == {
            "trg_sms_email_billable_events_immutable",
            "trg_sms_email_billable_events_no_truncate",
        }
        assert {r[0] for r in c.execute(q, {"t": "sms_billing_usage_reports"})} == {
            "trg_sms_billing_usage_reports_guard",
            "trg_sms_billing_usage_reports_no_delete",
        }
    enterprise_alembic(url, "downgrade", "0026")
    with eng.connect() as c:
        assert (
            c.execute(
                text("select count(*) from pg_proc where proname like 'sms_billing_%'")
            ).scalar()
            == 0
        )
    eng.dispose()
    curl = make_db("central")
    central_alembic(curl, "upgrade", "head")
    ceng = create_engine(curl)
    with ceng.connect() as c:
        assert {r[0] for r in c.execute(q, {"t": "billing_usage_reports"})} == {
            "trg_billing_usage_reports_immutable",
            "trg_billing_usage_reports_no_truncate",
        }
    ceng.dispose()
