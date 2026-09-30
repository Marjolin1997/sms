"""M1b: kolona `enterprise_id`, dual-write i centralizuar, backfill në batch, invariant konsistence,
trigger-i i pandryshueshëm i consent-it. `owner_ref` mbetet burimi i sjelljes; asgjë nuk lexon ende
`enterprise_id`."""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.tenancy import TenantMismatch
from app.models.contacts import ConsentEvent, Contact
from app.models.enterprise import Enterprise
from app.models.sending import Message
from app.models.tenant import TenantOwned
from app.services import consent
from app.services import contacts as contacts_svc
from app.services import enterprises as svc
from app.services import wallet as wallets
from tests.test_pipeline import send, world  # noqa: F401

NOW = datetime.now(UTC).isoformat()
PG_URL = os.environ.get("SMS_TEST_DATABASE_URL", "")


def ent(db, owner):
    return db.scalar(select(Enterprise).where(Enterprise.owner_ref == owner))


# --- Dual-write i centralizuar ---------------------------------------------------------------


def test_new_rows_get_enterprise_id_automatically_without_service_changes(db, world):  # noqa: F811
    w = wallets.create_wallet(db, "newco", "EUR")
    c, _ = contacts_svc.upsert(db, "newco", phone="+355691230077")
    db.commit()
    e = ent(db, "newco")
    assert e is not None  # tenant i ri → Enterprise i krijuar automatikisht
    assert w.enterprise_id == e.id and c.enterprise_id == e.id


def test_existing_enterprise_is_reused_not_duplicated(db, world):  # noqa: F811
    db.add(Enterprise(owner_ref="acme"))
    db.commit()
    eid = ent(db, "acme").id
    wallets.create_wallet(db, "acme", "EUR")
    contacts_svc.upsert(db, "acme", phone="+355691230078")
    db.commit()
    assert (
        db.scalar(
            select(text("count(*)")).select_from(Enterprise).where(Enterprise.owner_ref == "acme")
        )
        == 1
    )
    assert {r.enterprise_id for r in db.scalars(select(Contact))} == {eid}


def test_full_sms_flow_carries_the_same_enterprise_id_everywhere(db, world):  # noqa: F811
    m = send(db, key="k1")
    eid = ent(db, "c1").id
    assert m.enterprise_id == eid
    for model in (Message, Contact):
        assert all(r.enterprise_id in (eid, None) for r in db.scalars(select(model)))
    w, _ = world
    assert db.get(type(w), w.id).enterprise_id == eid


def test_staff_keys_without_owner_ref_have_no_enterprise(db):
    from app.services import apikeys

    k, _ = apikeys.create_key(db, "s", "superadmin", None, "t")
    c, _ = apikeys.create_key(db, "c", "client", "acme", "t")
    db.commit()
    assert k.enterprise_id is None and c.enterprise_id == ent(db, "acme").id


def test_every_tenant_model_uses_the_mixin_and_table_list_matches():
    import app.models  # noqa: F401
    from app.core.db import Base

    mixed = {
        m.class_.__tablename__ for m in Base.registry.mappers if issubclass(m.class_, TenantOwned)
    }
    with_owner = {
        t.name
        for t in Base.metadata.sorted_tables
        if "owner_ref" in t.columns and t.name != "sms_enterprises"
    }
    assert mixed == with_owner == set(svc.LEGACY_OWNER_TABLES)


def test_explicit_mismatching_enterprise_id_is_rejected(db):
    a = Enterprise(owner_ref="a")
    b = Enterprise(owner_ref="b")
    db.add_all([a, b])
    db.commit()
    db.add(Contact(owner_ref="a", enterprise_id=b.id, phone="355691230001"))
    with pytest.raises(TenantMismatch):
        db.commit()
    db.rollback()


def test_explicit_matching_enterprise_id_is_accepted(db):
    a = Enterprise(owner_ref="a")
    db.add(a)
    db.commit()
    db.add(Contact(owner_ref="a", enterprise_id=a.id, phone="355691230002"))
    db.commit()
    assert db.scalar(select(Contact)).enterprise_id == a.id


def test_changing_owner_ref_re_resolves_enterprise_id(db):
    c, _ = contacts_svc.upsert(db, "old", phone="+355691230003")
    db.commit()
    c.owner_ref = "moved"
    db.commit()
    assert c.enterprise_id == ent(db, "moved").id != ent(db, "old").id


# --- Anomalitë: sjellja e sistemit nuk ndryshon (NULL, jo përjashtim) ---------------------


@pytest.mark.parametrize("bad", ["", " acme", "acme ", "a\nb"])
def test_anomalous_owner_ref_is_stored_as_before_with_null_enterprise_id(db, bad):
    w = wallets.create_wallet(db, bad, "EUR")
    db.commit()
    assert w.owner_ref == bad and w.enterprise_id is None
    assert svc.count(db) == 0  # asnjë Enterprise nuk krijohet nga anomalia


def test_case_variant_of_an_existing_enterprise_is_stored_with_null_and_reported(db):
    db.add(Enterprise(owner_ref="CLIENT_A"))
    db.commit()
    w = wallets.create_wallet(db, "client_a", "EUR")  # sjellje e njëjtë si para M1b: ppranohet
    db.commit()
    assert w.owner_ref == "client_a" and w.enterprise_id is None
    assert svc.count(db) == 1  # nuk krijohet/bashkohet asgjë
    assert svc.check_consistency(db).unbackfilled == 1  # por raportohet


def test_strict_mode_turns_anomalies_into_errors(db, monkeypatch):
    monkeypatch.setattr(settings, "enterprise_dual_write_strict", True)
    with pytest.raises(TenantMismatch):
        wallets.create_wallet(db, " bad", "EUR")
    db.rollback()


def test_dual_write_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "enterprise_dual_write", False)
    w = wallets.create_wallet(db, "quiet", "EUR")
    db.commit()
    assert w.enterprise_id is None and svc.count(db) == 0


def test_concurrent_creation_of_the_same_tenant_yields_one_enterprise():
    """Dy sesione krijojnë njëkohësisht të njëjtin owner_ref të ri: një Enterprise, dy rreshta të lidhur."""
    import threading

    ids: list = []
    barrier = threading.Barrier(2)

    def run(n):
        with SessionLocal() as s:
            barrier.wait()
            w = wallets.create_wallet(s, "racer", f"E{n}U")
            s.commit()
            ids.append(w.enterprise_id)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(text("count(*)"))
                .select_from(Enterprise)
                .where(Enterprise.owner_ref == "racer")
            )
            == 1
        )
    assert len(ids) == 2 and ids[0] == ids[1] and ids[0] is not None


# --- Kontrolli i konsistencës ------------------------------------------------------------------


def test_consistency_report_detects_mismatch_and_null(db):
    wallets.create_wallet(db, "a", "EUR")
    wallets.create_wallet(db, "b", "EUR")
    db.commit()
    assert svc.check_consistency(db).complete
    a_id, b_id = ent(db, "a").id, ent(db, "b").id
    db.execute(
        text("update sms_wallets set enterprise_id = :x where owner_ref = 'a'"), {"x": b_id.hex}
    )  # i gabuar
    db.execute(text("update sms_wallets set enterprise_id = NULL where owner_ref = 'b'"))
    db.commit()
    rep = svc.check_consistency(db)
    row = next(t for t in rep.tables if t.table == "sms_wallets")
    assert (row.mismatched, row.null_enterprise_id) == (1, 1) and not rep.ok and not rep.complete
    assert a_id != b_id


# --- Backfill në batch ---------------------------------------------------------------------------


def _legacy_rows(db, n_tenants=3, per=7):
    """Rreshta pa enterprise_id (si para M1b): dual-write fiket për t'i krijuar 'legacy'."""
    settings.enterprise_dual_write = False
    try:
        for t in range(n_tenants):
            for i in range(per):
                contacts_svc.upsert(db, f"t{t}", phone=f"+3556912{t}{i:04d}")
            wallets.create_wallet(db, f"t{t}", "EUR")
        db.commit()
    finally:
        settings.enterprise_dual_write = True


def test_backfill_fills_everything_in_batches_and_is_idempotent(db):
    _legacy_rows(db)
    assert svc.check_consistency(db).unbackfilled == 3 * 7 + 3
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    first = svc.backfill_enterprise_ids(factory, batch=5)
    assert first["sms_contacts"] == 21 and first["sms_wallets"] == 3
    db.expire_all()
    assert svc.check_consistency(db).complete
    again = svc.backfill_enterprise_ids(factory, batch=5)
    assert sum(again.values()) == 0  # idempotent
    assert svc.count(db) == 3


def test_backfill_is_resumable_and_never_touches_other_columns(db):
    _legacy_rows(db, n_tenants=1, per=12)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    before = db.execute(text("select id, owner_ref, phone from sms_contacts order by id")).all()
    svc.backfill_enterprise_ids(factory, batch=4, tables=("sms_contacts",))
    assert (
        db.execute(text("select id, owner_ref, phone from sms_contacts order by id")).all()
        == before
    )
    assert svc.check_consistency(db).ok


def test_backfill_leaves_anomalies_null_and_terminates(db):
    _legacy_rows(db, n_tenants=1, per=3)
    settings.enterprise_dual_write = False
    try:
        db.add(Contact(owner_ref="t0 ", phone="355691239999"))  # me hapësirë: anomali
        db.commit()
    finally:
        settings.enterprise_dual_write = True
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with pytest.raises(svc.InvalidOwnerRef):  # auditi ndalon (nuk hamendëson kush është t0)
        svc.backfill_enterprise_ids(factory)


def test_backfill_script_check_mode(db, tmp_path):
    _legacy_rows(db, n_tenants=1, per=2)
    env = {**os.environ}
    r = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_enterprise_id", "--check"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (
        r.returncode == 0 and "pa enterprise_id" in r.stdout
    )  # jo-përputhje = 0 (vetëm të pa-plotësuara)
    r = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_enterprise_id", "--batch", "10"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and "jo-përputhje=0" in r.stdout and "pa enterprise_id=0" in r.stdout


# --- Migrimi 0019 ---------------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "postgres"])
def at_0018(request, tmp_path):
    if request.param == "postgres":
        if not PG_URL.startswith("postgresql"):
            pytest.skip("needs PostgreSQL")
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        name = f"sms_m1b_{uuid.uuid4().hex[:8]}"
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    else:
        url, name = f"sqlite:///{tmp_path / 'm.db'}", None
    alembic(url, "upgrade", "0018")
    yield url
    if name:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def alembic(url, *args):
    env = {**os.environ, "SMS_DATABASE_URL": url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r


def test_0019_adds_nullable_indexed_column_to_all_21_tables_and_nothing_else(at_0018):
    eng = create_engine(at_0018)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_wallets (owner_ref, currency, created_at) values ('acme','EUR',:t)"
            ),
            {"t": NOW},
        )
    insp = inspect(eng)
    before = {
        t: [(c["name"], str(c["type"])) for c in insp.get_columns(t)]
        for t in insp.get_table_names()
    }
    alembic(at_0018, "upgrade", "head")
    insp = inspect(create_engine(at_0018))
    for t in svc.LEGACY_OWNER_TABLES:
        cols = {c["name"]: c for c in insp.get_columns(t)}
        assert cols["enterprise_id"]["nullable"] is True
        assert f"ix_{t}_enterprise_id" in {i["name"] for i in insp.get_indexes(t)}
        assert insp.get_foreign_keys(t) == [
            f for f in insp.get_foreign_keys(t) if "enterprise" not in f["referred_table"]
        ]
        assert [(n, str(x["type"])) for n, x in cols.items() if n != "enterprise_id"] == before[t]
    for t in set(before) - set(svc.LEGACY_OWNER_TABLES):
        assert [(c["name"], str(c["type"])) for c in insp.get_columns(t)] == before[
            t
        ]  # tabelat e tjera të paprekura
    with create_engine(at_0018).connect() as c:
        assert c.execute(text("select owner_ref, enterprise_id from sms_wallets")).all() == [
            ("acme", None)
        ]  # pa backfill këtu


def test_0019_downgrade_restores_and_reupgrade_works(at_0018):
    alembic(at_0018, "upgrade", "head")
    alembic(at_0018, "downgrade", "0018")
    insp = inspect(create_engine(at_0018))
    assert all(
        "enterprise_id" not in {c["name"] for c in insp.get_columns(t)}
        for t in svc.LEGACY_OWNER_TABLES
    )
    alembic(at_0018, "upgrade", "head")


def test_migration_table_list_matches_service_list():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "alembic/versions/0019_enterprise_id_columns.py"
    spec = importlib.util.spec_from_file_location("m0019", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.TABLES) == set(svc.LEGACY_OWNER_TABLES)


# --- Trigger-i i consent-it (PostgreSQL) ------------------------------------------------------------


@pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="needs PostgreSQL")
def test_consent_events_stay_immutable_except_the_one_enterprise_id_backfill(at_0018_pg):
    url = at_0018_pg
    alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_enterprises (id, owner_ref, status, created_at, updated_at) values (:i,'acme','active',now(),now())"
            ),
            {"i": uuid.uuid4()},
        )
        c.execute(
            text(
                "insert into sms_consent_events (owner_ref, channel, address_hash, action, reason, source, actor, created_at) "
                "values ('acme','sms','h','OPT_IN','r','s','a',now())"
            )
        )
    eid = eng.connect().execute(text("select id from sms_enterprises")).scalar()

    def attempt(sql, **p):
        with eng.connect() as c:
            with pytest.raises(DBAPIError) as e:
                c.execute(text(sql), p)
                c.commit()
            return str(e.value)

    assert "append-only" in attempt(
        "update sms_consent_events set reason = 'x'"
    )  # përmbajtja e prekur
    assert "append-only" in attempt("update sms_consent_events set owner_ref = 'evil'")
    assert "append-only" in attempt(
        "update sms_consent_events set enterprise_id = :e, reason = 'x'", e=eid
    )  # dy kolona
    assert "append-only" in attempt("delete from sms_consent_events")
    with eng.begin() as c:  # përjashtimi i vetëm: enterprise_id NULL → vlerë
        c.execute(text("update sms_consent_events set enterprise_id = :e"), {"e": eid})
    assert "append-only" in attempt(
        "update sms_consent_events set enterprise_id = NULL"
    )  # s'kthehet mbrapsht
    assert "append-only" in attempt(
        "update sms_consent_events set enterprise_id = :e", e=uuid.uuid4()
    )  # s'ndërrohet


@pytest.fixture
def at_0018_pg():
    if not PG_URL.startswith("postgresql"):
        pytest.skip("needs PostgreSQL")
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    name = f"sms_m1b_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    alembic(url, "upgrade", "0018")
    yield url
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="needs PostgreSQL")
def test_consent_orm_is_still_append_only_and_new_events_get_enterprise_id(db):
    consent.record(db, "acme", "sms", "+355691230001", "opt_in", "x", "form", "u", "evidence")
    db.commit()
    ev = db.scalars(select(ConsentEvent)).one()
    assert ev.enterprise_id == ent(db, "acme").id
    ev.reason = "tamper"
    from app.models.contacts import ConsentImmutableError

    with pytest.raises(ConsentImmutableError):
        db.commit()
    db.rollback()


_ = Session
