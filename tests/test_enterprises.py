"""M1a: regjistri i Enterprise-ve. Migrim strikt additiv, backfill nga `owner_ref` legacy,
auditim anomalish, shërbimi `enterprises.*`. Sjellja e sistemit mbetet identike."""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.models.enterprise import Enterprise
from app.services import enterprises as svc
from app.services.wallet import NotFound

NOW = datetime.now(UTC).isoformat()
PG_URL = os.environ.get("SMS_TEST_DATABASE_URL", "")


# --- Infrastrukturë: bazë e re e ngritur me Alembic deri te 0017 -----------------------


@pytest.fixture(params=["sqlite", "postgres"])
def legacy(request, tmp_path):
    if request.param == "postgres":
        if not PG_URL.startswith("postgresql"):
            pytest.skip("needs PostgreSQL")
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
        name = f"sms_m1a_{uuid.uuid4().hex[:8]}"
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    else:
        url = f"sqlite:///{tmp_path / 'legacy.db'}"
        name = None
    alembic(url, "upgrade", "0017")
    yield url
    if name:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def alembic(url, *args, expect_ok=True):
    env = {**os.environ, "SMS_DATABASE_URL": url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
    )
    if expect_ok:
        assert r.returncode == 0, r.stderr
    return r


def seed(url, owners, staff_keys=1):
    """Rreshta legacy në disa tabela (wallets, keywords, contact_lists, api_keys)."""
    eng = create_engine(url)
    with eng.begin() as c:
        for i, o in enumerate(owners):
            c.execute(
                text(
                    "insert into sms_wallets (owner_ref, currency, created_at) values (:o, 'EUR', :t)"
                ),
                {"o": o, "t": NOW},
            )
            c.execute(
                text(
                    "insert into sms_keywords (owner_ref, keyword, created_at) values (:o, 'help', :t)"
                ),
                {"o": o, "t": NOW},
            )
            c.execute(
                text(
                    "insert into sms_contact_lists (owner_ref, name, created_at) values (:o, 'L', :t)"
                ),
                {"o": o, "t": NOW},
            )
            c.execute(
                text(
                    "insert into sms_api_keys (prefix, key_hash, name, role, owner_ref, status, created_by, created_at)"
                    " values (:p, 'h', 'k', 'client', :o, 'ACTIVE', 't', :t)"
                ),
                {"p": f"p{i:07d}", "o": o, "t": NOW},
            )
        for j in range(staff_keys):  # çelës stafi: owner_ref NULL (legjitim)
            c.execute(
                text(
                    "insert into sms_api_keys (prefix, key_hash, name, role, owner_ref, status, created_by, created_at)"
                    " values (:p, 'h', 's', 'superadmin', NULL, 'ACTIVE', 't', :t)"
                ),
                {"p": f"s{j:07d}", "t": NOW},
            )
    return eng


def fetch(eng, sql):
    with eng.connect() as c:
        return c.execute(text(sql)).all()


# --- Migrimi: backfill, invariant, UUID të qëndrueshme ----------------------------------


def test_each_existing_owner_ref_becomes_exactly_one_enterprise(legacy):
    eng = seed(legacy, ["acme", "globex", "Initech"])
    alembic(legacy, "upgrade", "0018")
    rows = fetch(
        eng,
        "select owner_ref, status, legal_name, short_name, external_id from sms_enterprises order by owner_ref",
    )
    assert [r[0] for r in rows] == ["Initech", "acme", "globex"]
    assert all(
        r[1] == "active" and r[2] is None and r[3] is None and r[4] is None for r in rows
    )  # pa supozime


def test_invariant_distinct_valid_owner_refs_equal_enterprises(legacy):
    eng = seed(legacy, ["a1", "b2", "c3", "d4"], staff_keys=3)
    distinct = fetch(
        eng,
        "select count(*) from (select owner_ref from sms_wallets union select owner_ref from sms_api_keys "
        "union select owner_ref from sms_keywords union select owner_ref from sms_contact_lists) x where owner_ref is not null",
    )[0][0]
    alembic(legacy, "upgrade", "0018")
    assert fetch(eng, "select count(*) from sms_enterprises")[0][0] == distinct == 4


def test_null_owner_ref_of_staff_keys_is_ignored_not_an_enterprise(legacy):
    eng = seed(legacy, ["acme"], staff_keys=2)
    alembic(legacy, "upgrade", "0018")
    assert fetch(eng, "select owner_ref from sms_enterprises") == [("acme",)]


def test_empty_database_migrates_with_zero_enterprises(legacy):
    alembic(legacy, "upgrade", "0018")
    assert fetch(create_engine(legacy), "select count(*) from sms_enterprises")[0][0] == 0


def test_uuids_are_persisted_and_unique(legacy):
    eng = seed(legacy, ["acme", "globex"])
    alembic(legacy, "upgrade", "0018")
    ids = [r[0] for r in fetch(eng, "select id from sms_enterprises")]
    assert len(set(map(str, ids))) == 2 and all(i is not None for i in ids)


def test_migration_touches_only_the_new_table(legacy):
    """Prova e additivitetit: skema e çdo tabele tjetër është identike para dhe pas 0018."""
    eng = create_engine(legacy)

    def snapshot():
        insp = inspect(eng)
        out = {}
        for t in insp.get_table_names():
            if t == "sms_enterprises":
                continue
            out[t] = (
                [(c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(t)],
                sorted(str(i["name"]) + str(i["column_names"]) for i in insp.get_indexes(t)),
                sorted(
                    str(f["constrained_columns"]) + f["referred_table"]
                    for f in insp.get_foreign_keys(t)
                ),
            )
        return out

    seed(legacy, ["acme"])
    before = snapshot()
    alembic(legacy, "upgrade", "0018")
    assert snapshot() == before
    assert "sms_enterprises" in inspect(eng).get_table_names()


def test_data_of_legacy_tables_is_untouched(legacy):
    eng = seed(legacy, ["acme", "globex"])
    before = fetch(eng, "select * from sms_wallets order by id")
    alembic(legacy, "upgrade", "0018")
    assert fetch(eng, "select * from sms_wallets order by id") == before


def test_downgrade_and_upgrade_again(legacy):
    eng = seed(legacy, ["acme"])
    alembic(legacy, "upgrade", "0018")
    alembic(legacy, "downgrade", "0017")
    assert "sms_enterprises" not in inspect(create_engine(legacy)).get_table_names()
    assert fetch(eng, "select owner_ref from sms_wallets") == [
        ("acme",)
    ]  # të dhënat legacy të paprekura
    alembic(legacy, "upgrade", "0018")
    assert fetch(eng, "select owner_ref from sms_enterprises") == [("acme",)]


# --- Anomalitë: migrimi ndalon, s'bashkon asgjë -----------------------------------------


@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        ("", "bosh"),
        (" acme", "hapësira"),
        ("acme ", "hapësira"),
        ("ac\nme", "kontrolli"),
        ("x" * 65, "gjatë"),
    ],
)
def test_anomalies_stop_the_migration_without_changing_anything(legacy, bad, needle):
    eng = seed(legacy, ["good"])
    if len(bad) > 64 and legacy.startswith("postgresql"):
        pytest.skip("kolona VARCHAR(64) në PG nuk lejon >64")
    with eng.begin() as c:
        c.execute(
            text("insert into sms_keywords (owner_ref, keyword, created_at) values (:o, 'x', :t)"),
            {"o": bad, "t": NOW},
        )
    r = alembic(legacy, "upgrade", "0018", expect_ok=False)
    assert r.returncode != 0 and "NDALOI" in r.stderr and needle in r.stderr
    assert "sms_enterprises" not in inspect(eng).get_table_names()  # asgjë nuk u krijua
    assert fetch(eng, "select count(*) from sms_wallets")[0][0] == 1


def test_case_variants_are_never_merged_automatically(legacy):
    eng = seed(legacy, ["CLIENT_A"])
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_keywords (owner_ref, keyword, created_at) values ('client_a', 'y', :t)"
            ),
            {"t": NOW},
        )
    r = alembic(legacy, "upgrade", "0018", expect_ok=False)
    assert (
        r.returncode != 0
        and "shkronjat" in r.stderr
        and "CLIENT_A" in r.stderr
        and "client_a" in r.stderr
    )
    assert "sms_enterprises" not in inspect(eng).get_table_names()


def test_separator_collisions_only_warn_and_do_not_stop(legacy):
    seed(legacy, ["client-a", "client_a"])
    r = alembic(legacy, "upgrade", "0018")
    assert "WARNING" in r.stdout and "client-a" in r.stdout
    eng = create_engine(legacy)
    assert {x[0] for x in fetch(eng, "select owner_ref from sms_enterprises")} == {
        "client-a",
        "client_a",
    }  # dy tenant-e të veçantë


def test_migration_runs_the_same_audit_as_the_service(legacy):
    """Auditi runtime (scripts/enterprises_audit) dhe ai i migrimit japin të njëjtin verdikt."""
    eng = seed(legacy, ["ok1", "ok2"])
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_keywords (owner_ref, keyword, created_at) values ('Ok1', 'z', :t)"
            ),
            {"t": NOW},
        )
    from sqlalchemy.orm import Session

    with Session(eng) as db:
        audit = svc.audit_owner_refs(db)
    assert not audit.ok and any("shkronjat" in e for e in audit.errors)
    assert alembic(legacy, "upgrade", "0018", expect_ok=False).returncode != 0


# --- Shërbimi (mbi bazën e testeve) ------------------------------------------------------


def test_for_owner_ref_returns_existing_and_none_for_unknown(db):
    e = Enterprise(owner_ref="acme")
    db.add(e)
    db.commit()
    assert svc.for_owner_ref(db, "acme").id == e.id
    assert svc.for_owner_ref(db, "unknown") is None  # nuk krijohet asgjë
    assert svc.count(db) == 1
    with pytest.raises(svc.EnterpriseNotFound):
        svc.require_for_owner_ref(db, "unknown")
    assert issubclass(svc.EnterpriseNotFound, NotFound)


def test_lookup_is_exact_no_silent_normalization(db):
    db.add(Enterprise(owner_ref="Acme"))
    db.commit()
    assert svc.for_owner_ref(db, "acme") is None  # shkronjat të ndryshme = nuk përputhet
    assert svc.for_owner_ref(db, "Acme") is not None


@pytest.mark.parametrize("bad", [None, "", " ", " acme", "acme ", "\tacme", 5])
def test_blank_null_or_padded_owner_ref_is_rejected_explicitly(db, bad):
    with pytest.raises(svc.InvalidOwnerRef):
        svc.for_owner_ref(db, bad)
    with pytest.raises(svc.InvalidOwnerRef):
        svc.require_for_owner_ref(db, bad)


def test_backfill_is_idempotent_and_keeps_uuids(db, monkeypatch):
    from app.core.config import settings
    from app.services import wallet as wallets

    monkeypatch.setattr(
        settings, "enterprise_dual_write", False
    )  # simulon të dhëna legacy (para M1b)

    for o in ("acme", "globex"):
        wallets.create_wallet(db, o, "EUR")
    db.commit()
    first = svc.backfill_missing(db)
    db.commit()
    assert (first.created, first.already_present) == (2, 0)
    ids = {e.owner_ref: e.id for e in db.scalars(select(Enterprise))}
    again = svc.backfill_missing(db)
    db.commit()
    assert (again.created, again.already_present) == (0, 2)
    assert {
        e.owner_ref: e.id for e in db.scalars(select(Enterprise))
    } == ids  # UUID të pandryshuara
    wallets.create_wallet(db, "initech", "EUR")
    db.commit()
    third = svc.backfill_missing(db)  # kap vetëm të rinjtë
    db.commit()
    assert (third.created, third.already_present) == (1, 2)
    assert svc.count(db) == 3


def test_backfill_refuses_when_anomalies_exist_and_writes_nothing(db, monkeypatch):
    from app.core.config import settings
    from app.services import wallet as wallets

    monkeypatch.setattr(settings, "enterprise_dual_write", False)

    wallets.create_wallet(db, "Acme", "EUR")
    wallets.create_wallet(db, "acme", "EUR")
    db.commit()
    with pytest.raises(svc.InvalidOwnerRef, match="shkronjat"):
        svc.backfill_missing(db)
    assert svc.count(db) == 0


def test_database_rejects_duplicate_and_case_variant_enterprises(db):
    db.add(Enterprise(owner_ref="acme"))
    db.commit()
    db.add(Enterprise(owner_ref="acme"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(Enterprise(owner_ref="ACME"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_external_id_is_unique_only_when_present(db):
    db.add_all(
        [
            Enterprise(owner_ref="a"),
            Enterprise(owner_ref="b"),
            Enterprise(owner_ref="c", external_id="X1"),
        ]
    )
    db.commit()  # dy NULL external_id lejohen
    db.add(Enterprise(owner_ref="d", external_id="X1"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_legacy_table_list_matches_the_models():
    """Roje kundër driftit: çdo tabelë me `owner_ref` duhet të jetë në listën e migrimit/shërbimit."""
    import importlib.util
    from pathlib import Path

    import app.models  # noqa: F401
    from app.core.db import Base

    in_models = {
        t.name
        for t in Base.metadata.sorted_tables
        if "owner_ref" in t.columns and t.name != "sms_enterprises"
    }
    assert set(svc.LEGACY_OWNER_TABLES) == in_models
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0018_enterprises.py"
    spec = importlib.util.spec_from_file_location("m0018", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.TABLES) == in_models


def test_enterprise_id_is_written_only_by_the_centralized_hook():
    """M1b: asnjë shërbim/API nuk e shkruan `enterprise_id`; vetëm hook-u `core/tenancy.py`, resolveri
    `services/enterprises.py` dhe modelet. Asnjë modul nuk e LEXON ende për sjellje (M1c)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "app"
    allowed = {"core/tenancy.py", "services/enterprises.py", "models/tenant.py", "models/enterprise.py",
               "models/__init__.py", "core/config.py"}  # fmt: skip
    offenders = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if p.relative_to(root).as_posix() not in allowed and "enterprise_id" in p.read_text()
    ]
    assert offenders == []
