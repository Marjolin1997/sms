"""Drill restore LOKAL/STAGING-grade për të dyja bazat logjike (M11-I/F-05A). Punon VETËM me baza të përkohshme që krijon vetë.

    ADMIN_URL=postgresql://user:...@host/postgres python -m scripts.recovery_drill [--workdir DIR]

Për Enterprise dhe Central, me radhë:
  1) krijon bazë burim të përkohshme, migron deri në kokë, mbush të dhëna përfaqësuese (me ledger/para);
  2) gishti i burimit (rreshta, trigger-a, indekse, kufizime, sekuenca, revizion);
  3) scripts/backup.sh → dump + .sha256 + .meta.json;
  4) FSHIN bazën burim (provon që restore nuk varet nga burimi);
  5) scripts/restore.sh në një bazë të RE; kontroll kokë migrimesh; verifikim ledger (Enterprise: verify_ledger; Central: central-ledger);
  6) gishti i bazës së rikthyer duhet të jetë IDENTIK me atë të burimit;
  7) fshin bazat e restore-it. Asnjëherë nuk prek baza që nuk i ka krijuar vetë (prefiksi `f05drill_`); URL/kredencialet nuk printohen.
Kodi: 0 të gjitha hapat kaluan · 1 dështoi një hap. Kjo NUK është PITR dhe NUK është provë prodhimi."""

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "f05drill_"
PII_KEY = "drill-only-hmac-key-0123456789abcdef0123456789"
SECRETS_KEY = (
    "wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ="  # çelës Fernet vetëm për seed-in e drill-it
)


class DrillError(Exception):
    pass


def _url(admin: URL, db: str, driver: str) -> str:
    return admin.set(drivername=driver, database=db).render_as_string(hide_password=False)


def _run(
    argv: list[str], env: dict[str, str], what: str, ok_codes=(0,)
) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode not in ok_codes:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
        raise DrillError(f"{what} failed (exit {r.returncode}):\n{_scrub(tail, env)}")
    return r


def _scrub(s: str, env: dict[str, str]) -> str:
    for k in (
        "PGPASSWORD",
        "ADMIN_URL",
        "DATABASE_URL",
        "SMS_DATABASE_URL",
        "CENTRAL_DATABASE_URL",
    ):
        v = env.get(k)
        if v:
            s = s.replace(v, "<redacted>")
    pw = make_url(env["ADMIN_URL"]).password if env.get("ADMIN_URL") else None
    return s.replace(pw, "<redacted>") if pw else s


class Drill:
    def __init__(self, admin_url: str, workdir: Path):
        self.admin = make_url(admin_url)
        self.admin_db = self.admin.database
        self.workdir = workdir
        self.tag = secrets.token_hex(3)
        self.created: list[str] = []
        self.eng = create_engine(
            self.admin.set(drivername="postgresql+psycopg"), isolation_level="AUTOCOMMIT"
        )
        self.base_env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "ADMIN_URL": _url(self.admin, self.admin_db, "postgresql"),
            "SMS_PII_HMAC_KEY": PII_KEY,
            "SMS_SECRETS_KEY": SECRETS_KEY,
        }
        for k in ("SMS_DATABASE_URL", "CENTRAL_DATABASE_URL", "PGPASSWORD", "PGDATABASE"):
            self.base_env.pop(k, None)

    # ------------------------------------------------------------------ baza të përkohshme
    def name(self, label: str, role: str) -> str:
        mid = "central" if label == "central" else "ent"
        return f"{PREFIX}{mid}_{role}_{self.tag}"

    def create_db(self, db: str) -> None:
        assert db.startswith(PREFIX) and db != self.admin_db
        with self.eng.connect() as c:
            c.execute(text(f'create database "{db}"'))
        self.created.append(db)

    def drop_db(self, db: str) -> None:
        assert db.startswith(PREFIX) and db != self.admin_db  # asnjëherë një bazë që s'e krijuam ne
        with self.eng.connect() as c:
            c.execute(text(f'drop database if exists "{db}" with (force)'))
        if db in self.created:
            self.created.remove(db)

    def db_exists(self, db: str) -> bool:
        with self.eng.connect() as c:
            return bool(
                c.execute(text("select 1 from pg_database where datname=:n"), {"n": db}).first()
            )

    def cleanup(self) -> None:
        for db in list(self.created):
            self.drop_db(db)

    # ------------------------------------------------------------------ hapat
    def env_for(self, label: str, db: str) -> dict[str, str]:
        e = dict(self.base_env)
        key = "CENTRAL_DATABASE_URL" if label == "central" else "SMS_DATABASE_URL"
        e[key] = _url(self.admin, db, "postgresql+psycopg")
        return e

    def migrate_and_seed(self, label: str, db: str) -> None:
        env = self.env_for(label, db)
        if label == "central":
            _run(
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "apps/central/alembic.ini",
                    "upgrade",
                    "head",
                ],
                env,
                "central migrate",
            )
            seed_central(env["CENTRAL_DATABASE_URL"])
        else:
            _run([sys.executable, "-m", "alembic", "upgrade", "head"], env, "enterprise migrate")
            _run(
                [sys.executable, "-m", "scripts.seed_demo"], env, "enterprise seed"
            )  # wallet + ledger + fatura

    def verify(self, label: str, db: str, out: str | None = None) -> None:
        env = self.env_for(label, db)
        _run(
            [sys.executable, "-m", "scripts.recovery_verify", "head", "--label", label],
            env,
            f"{label} head check",
        )
        _run([sys.executable, "-m", "scripts.recovery_verify", "central-ledger"] if label == "central"
             else [sys.executable, "-m", "scripts.verify_ledger"], env, f"{label} ledger verification")  # fmt: skip
        if out:
            _run(
                [
                    sys.executable,
                    "-m",
                    "scripts.recovery_verify",
                    "fingerprint",
                    "--label",
                    label,
                    "--out",
                    out,
                ],
                env,
                f"{label} fingerprint",
            )

    def one(self, label: str) -> None:
        src, rst = self.name(label, "src"), self.name(label, "rst")
        bdir = self.workdir / label
        bdir.mkdir(parents=True, exist_ok=True)
        fp_src, fp_rst = (
            str(bdir / "fingerprint-source.json"),
            str(bdir / "fingerprint-restored.json"),
        )

        self.create_db(src)
        self.migrate_and_seed(label, src)
        self.verify(label, src, fp_src)  # burimi vetë duhet të jetë i shëndetshëm para backup-it
        env = {**self.env_for(label, src), "BACKUP_LABEL": label, "BACKUP_DIR": str(bdir)}
        env["DATABASE_URL"] = _url(self.admin, src, "postgresql")
        r = _run(["scripts/backup.sh"], env, f"{label} backup")
        print(f"[{label}] backup: {_scrub(r.stdout.strip().splitlines()[-1], env)}")
        dump = sorted(bdir.glob(f"sms-{label}-*.dump"))[-1]

        self.drop_db(src)  # restore pa burimin
        assert not self.db_exists(src)
        r = _run(["scripts/restore.sh", str(dump), rst], self.base_env, f"{label} restore")
        self.created.append(rst)
        print(f"[{label}] {r.stdout.strip().splitlines()[-1].split('. Next')[0]}")

        self.verify(label, rst, fp_rst)
        cmp = _run(
            [sys.executable, "-m", "scripts.recovery_verify", "compare", fp_src, fp_rst],
            self.base_env,
            f"{label} fingerprint compare",
        )
        print(
            f"[{label}] head OK · ledger OK · {cmp.stdout.strip().splitlines()[-1]} (source dropped before restore)"
        )
        self.drop_db(rst)


def seed_central(url: str) -> None:
    """Të dhëna përfaqësuese Central: enterprise, produkt, 2 admin (maker/checker), llogari, pagesë e miratuar, grant."""
    from sqlalchemy.orm import sessionmaker

    from apps.central.services import credit_accounts as accts
    from apps.central.services import enterprises, grants, payments, users
    from apps.central.services import products as prod_svc

    eng = create_engine(url)
    pw = "drill only battery staple"
    with sessionmaker(bind=eng, expire_on_commit=False)() as s:
        ent = enterprises.create(s, "Drill Enterprise").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        a1 = users.create_user(s, "drill-a1@example.com", pw, "admin")
        a2 = users.create_user(s, "drill-a2@example.com", pw, "admin")
        s.commit()
        acct = accts.create(s, ent, sms, "EUR", a1)
        s.commit()
        p = payments.create(
            s, acct.id, "250.00", system="system:payment_import", external_reference="drill-ref-1"
        )
        payments.approve(s, p.id, a2)
        s.commit()
        grants.issue(s, acct.id, "75.00", idempotency_key="drill-grant-0001", actor=a1)
        s.commit()
    eng.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--workdir", help="ku shkruhen dump-et (parazgjedhje: dosje e përkohshme që fshihet)"
    )
    args = ap.parse_args(argv)
    admin = os.environ.get("ADMIN_URL")
    if not admin or not admin.startswith("postgresql"):
        print(
            "ERROR: set ADMIN_URL=postgresql://… (lidhje me bazën 'postgres' me CREATEDB)",
            file=sys.stderr,
        )
        return 2
    own_dir = args.workdir is None
    workdir = Path(args.workdir or tempfile.mkdtemp(prefix="f05drill-"))
    workdir.mkdir(parents=True, exist_ok=True)
    drill = Drill(admin, workdir)
    rc = 0
    try:
        for label in ("enterprise", "central"):
            drill.one(label)
        print(
            "DRILL OK: Enterprise + Central: backup → burimi i fshirë → restore në bazë të re → kokë + ledger + gisht identik"
        )
    except DrillError as exc:
        print(f"DRILL FAILED: {exc}", file=sys.stderr)
        rc = 1
    finally:
        drill.cleanup()
        if own_dir:
            shutil.rmtree(workdir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
