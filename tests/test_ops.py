"""Faza 16: kontrolle prodhimi, gatishmëria (readyz), request id, heartbeat i worker-it."""

import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import text

from app.core import readiness
from app.core.config import Settings
from app.core.db import engine

GOOD = dict(
    env="production",
    database_url="postgresql+psycopg://u:p@db/sms",
    pii_hmac_key="x" * 40,
    secrets_key="k" * 44,
    admin_api_key="",
    public_base_url="https://api.example.com",
    webhook_allow_http=False,
    email_provider="smtp",
    payment_provider="disabled",
)


def cfg(**kw):
    return Settings(_env_file=None, **(GOOD | kw))


def test_good_production_config_passes():
    assert cfg().production_problems() == []
    cfg().validate_production()


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"database_url": "sqlite:///x.db"}, "PostgreSQL"),
        ({"pii_hmac_key": "short"}, "PII_HMAC"),
        ({"secrets_key": ""}, "SECRETS_KEY"),
        ({"admin_api_key": "dev-admin-key-but-long-enough-xx"}, "weak"),
        ({"admin_api_key": "short"}, "weak"),
        ({"public_base_url": "http://api.example.com"}, "https"),
        ({"webhook_allow_http": True}, "WEBHOOK_ALLOW_HTTP"),
        ({"email_provider": "fake"}, "EMAIL_PROVIDER"),
        ({"payment_provider": "fake"}, "PAYMENT_PROVIDER"),
    ],
)
def test_unsafe_production_config_is_refused(override, needle):
    s = cfg(**override)
    assert any(needle in p for p in s.production_problems())
    with pytest.raises(RuntimeError, match="unsafe production configuration"):
        s.validate_production()


def test_development_is_not_validated():
    Settings(_env_file=None, env="development", database_url="sqlite:///x").validate_production()


def test_strong_admin_key_is_accepted():
    assert cfg(admin_api_key="r4ndom-Long-Bootstrap-Key-9f3a1c").production_problems() == []


def test_readyz_ok_without_alembic(client):
    assert client.get("/readyz").json() == {"status": "ready"}


def test_readyz_detects_schema_behind(client):
    with engine.begin() as c:
        c.execute(text("create table sms_alembic_version (version_num varchar(32) not null)"))
        c.execute(text("insert into sms_alembic_version values ('0001')"))
    try:
        r = client.get("/readyz")
        assert r.status_code == 503 and r.json()["status"] == "not_ready"
        assert "migrations" in r.json()["reason"] and "0001" not in r.text  # pa detaje të brendshme
        with engine.begin() as c:
            c.execute(
                text("update sms_alembic_version set version_num = :v"), {"v": readiness._head()}
            )
        assert client.get("/readyz").status_code == 200
    finally:
        with engine.begin() as c:
            c.execute(text("drop table sms_alembic_version"))


def test_readyz_blocks_enterprise_scoping_until_backfill_is_complete(client, monkeypatch, db):
    """M1c: rreshta pa enterprise_id do të fshiheshin nga tenant-ët → jo gati derisa të bëhet backfill."""
    from app.core.config import settings
    from app.services import contacts as contacts_svc

    monkeypatch.setattr(readiness, "_BACKFILL_TTL_S", 0.0)  # pa cache në test
    with engine.begin() as c:
        c.execute(text("create table sms_alembic_version (version_num varchar(32) not null)"))
        c.execute(text("insert into sms_alembic_version values (:v)"), {"v": readiness._head()})
    try:
        assert client.get("/readyz").status_code == 200
        contacts_svc.upsert(db, "acme", phone="+355691230003")
        db.commit()
        assert client.get("/readyz").status_code == 200  # dual-write e ka plotësuar
        db.execute(text("update sms_contacts set enterprise_id = null"))
        db.commit()
        r = client.get("/readyz")
        assert r.status_code == 503 and "backfill" in r.json()["reason"]
        monkeypatch.setattr(settings, "tenant_scoping", "owner_ref")  # rikthim i shprehur
        assert client.get("/readyz").status_code == 200
    finally:
        with engine.begin() as c:
            c.execute(text("drop table sms_alembic_version"))


def test_request_id_generated_and_echoed(client):
    r = client.get("/healthz")
    assert len(r.headers["x-request-id"]) == 32
    r = client.get("/healthz", headers={"X-Request-ID": "trace-123_ok.1"})
    assert r.headers["x-request-id"] == "trace-123_ok.1"
    r = client.get("/healthz", headers={"X-Request-ID": "bad id\twith space"})
    assert r.headers["x-request-id"] != "bad id\twith space"


def test_worker_heartbeat_health(tmp_path):
    hb = tmp_path / "alive"
    env = os.environ | {"SMS_WORKER_HEARTBEAT": str(hb)}

    def run(age="90"):
        return subprocess.run(
            [sys.executable, "-m", "scripts.worker_health", age], env=env, capture_output=True
        ).returncode

    assert run() != 0  # nuk ka nisur
    hb.touch()
    assert run() == 0
    old = time.time() - 300
    os.utime(hb, (old, old))
    assert run() != 0  # ngecur


def test_verify_ledger_passes_on_consistent_data(db):
    from app.services import wallet as wallets
    from scripts.verify_ledger import main

    w = wallets.create_wallet(db, "c1", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", wallets.TopupMethod.CASH).id)
    db.commit()
    assert main() == 0


def test_online_payments_can_be_disabled(db, monkeypatch):
    import pytest as _pytest

    from app.core.config import settings
    from app.services import payments
    from app.services import wallet as wallets

    monkeypatch.setattr(settings, "payment_provider", "disabled")
    w = wallets.create_wallet(db, "c1", "EUR")
    db.commit()
    with _pytest.raises(payments.PaymentsDisabled):
        payments.start_payment(db, "c1", "topup", "10", wallet_id=w.id)


def test_readiness_version_table_matches_alembic_env():
    """Rregullim: /readyz kontrollonte `alembic_version` por env.py përdor `sms_alembic_version`,
    ndaj kontrolli i skemës nuk zbatohej kurrë në prodhim."""
    import re
    from pathlib import Path

    env = (Path(__file__).resolve().parents[1] / "alembic/env.py").read_text()
    assert re.search(r'version_table="([^"]+)"', env).group(1) == readiness.VERSION_TABLE


def test_readyz_is_not_ready_when_a_real_migrated_schema_is_behind(client):
    """Rasti real: tabela e versionit me emrin që përdor Alembic, me version më të vjetër."""
    with engine.begin() as c:
        c.execute(text("create table sms_alembic_version (version_num varchar(32) not null)"))
        c.execute(text("insert into sms_alembic_version values ('0001')"))
    try:
        assert client.get("/readyz").status_code == 503
    finally:
        with engine.begin() as c:
            c.execute(text("drop table sms_alembic_version"))
