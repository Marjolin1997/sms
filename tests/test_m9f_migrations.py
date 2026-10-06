# ruff: noqa: F811
"""M9-f — migrimet 0021 (Central) dhe 0026 (Enterprise): vetëm trigger-a retention; up/down/up, pa ndryshim skeme."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.test_central import IS_PG, central_alembic, enterprise_alembic, make_db  # noqa: F401


def trig(eng, table):
    with eng.connect() as c:
        rows = c.execute(
            text("SELECT t.tgname, p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                 "WHERE t.tgrelid = CAST(:t AS regclass) AND NOT t.tgisinternal"), {"t": table},
        ).all()  # fmt: skip
    return dict(rows)


def test_central_0021_changes_no_schema_and_round_trips(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "0020")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    central_alembic(url, "upgrade", "0021")
    after = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert before == {k: v for k, v in after.items() if k in before} and set(after) == set(
        before
    )  # asnjë tabelë/kolonë e re
    if IS_PG and url.startswith("postgresql"):
        assert (
            trig(eng, "usage_reports")["trg_usage_reports_immutable"]
            == "central_usage_reports_guard"
        )
    central_alembic(url, "downgrade", "0020")
    if IS_PG and url.startswith("postgresql"):
        assert (
            trig(eng, "usage_reports")["trg_usage_reports_immutable"]
            == "central_money_forbid_mutation"
        )
    central_alembic(url, "upgrade", "head")
    eng.dispose()


def test_enterprise_0026_changes_no_schema_and_round_trips(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0025")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    enterprise_alembic(url, "upgrade", "head")
    after = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert before == after
    if url.startswith("postgresql"):
        assert (
            trig(eng, "sms_pricing_comparisons")["trg_sms_pricing_comparisons_immutable"]
            == "sms_pricing_comparisons_guard"
        )
        assert (
            trig(eng, "sms_usage_reports")["trg_sms_usage_reports_no_delete"]
            == "sms_usage_reports_delete_guard"
        )
    enterprise_alembic(url, "downgrade", "0025")
    if url.startswith("postgresql"):
        assert (
            trig(eng, "sms_pricing_comparisons")["trg_sms_pricing_comparisons_immutable"]
            == "sms_pricing_forbid"
        )
        assert (
            trig(eng, "sms_usage_reports")["trg_sms_usage_reports_no_delete"]
            == "sms_money_forbid_delete"
        )
    enterprise_alembic(url, "upgrade", "head")
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_heads_are_the_expected_single_revisions():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from tests.test_central import ROOT

    for ini, head in (("alembic.ini", "0026"), ("apps/central/alembic.ini", "0022")):
        cfg = Config(str(ROOT / ini))
        cfg.set_main_option(
            "script_location",
            str(ROOT / ("alembic" if ini == "alembic.ini" else "apps/central/migrations")),
        )
        assert ScriptDirectory.from_config(cfg).get_heads() == [head]
