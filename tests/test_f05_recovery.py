"""M11-I/F-05A — backup/restore/verifikim për Enterprise dhe Central në PostgreSQL të përkohshëm (pa prodhim, pa kredenciale të vërteta).

Provon skriptet siç dërgohen (backup.sh, restore.sh, recovery_verify.py, recovery_drill.py). Teste që s'duan DB ekzekutohen kudo;
të tjerat kërkojnë PostgreSQL (SMS_TEST_DATABASE_URL) me të drejtë CREATEDB dhe pg_dump/pg_restore/psql. NUK është provë PITR."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from scripts import recovery_drill as rd

ROOT = Path(__file__).resolve().parent.parent
BACKUP, RESTORE = ROOT / "scripts/backup.sh", ROOT / "scripts/restore.sh"
TEST_URL = os.environ.get("SMS_TEST_DATABASE_URL", "")
TOOLS = all(shutil.which(t) for t in ("pg_dump", "pg_restore", "psql", "sha256sum"))
needs_pg = pytest.mark.skipif(
    not (TEST_URL.startswith("postgresql") and TOOLS),
    reason="needs PostgreSQL + pg_dump/pg_restore/psql",
)


# Sekret sintetik, unik dhe me simbole: nuk mund të shfaqet legjitimisht te emra skedarësh/përmbajtje (ndryshe nga një fjalë e shkurtër si «sms»).
SECRET = "Zq7!vK9#xL2$mW4%pR8&tY1^uB5*"
LEAK_TOKENS = (
    "PGPASSWORD=",
    "SMS_DATABASE_URL=",
    "CENTRAL_DATABASE_URL=",
    "postgresql://",
    "postgres://",
)
URL_WITH_CREDENTIALS = re.compile(r"(?:[a-z][a-z0-9+.-]*)?://[^\s/@]*:[^\s/@]*@")


def assert_no_credential_leak(blob, secrets=()):
    """Kontroll semantik (jo nënstring i lirë): URL me kredenciale, ndryshore me sekret, URL të papërpunuara; sekretet e njohura vetëm nëse janë unike."""
    m = URL_WITH_CREDENTIALS.search(blob)
    assert m is None, f"credential-bearing URL in output: {m.group(0)[:12]}…"
    for tok in LEAK_TOKENS:
        assert tok not in blob, tok
    for sec in secrets:
        assert len(sec) >= 16, "use a unique, high-entropy secret for direct substring checks"
        assert sec not in blob and quote(sec, safe="") not in blob


def sh(args, env=None, cwd=ROOT, **kw):
    e = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(a) for a in args], cwd=cwd, env=e, capture_output=True, text=True, **kw
    )


def admin_url():
    return make_url(TEST_URL).set(database="postgres")


# ------------------------------------------------------------------ pa DB: parametra dhe gjendje e gabuar
def test_scripts_are_syntactically_valid_bash():
    for f in ("lib_pg.sh", "backup.sh", "restore.sh"):
        assert sh(["bash", "-n", ROOT / "scripts" / f]).returncode == 0, f


def test_backup_requires_a_valid_label_and_url():
    base = {"DATABASE_URL": "postgresql://u:pw@127.0.0.1:1/db"}
    assert sh([BACKUP], {**base, "BACKUP_LABEL": ""}).returncode != 0  # etiketa e detyrueshme
    r = sh([BACKUP], {**base, "BACKUP_LABEL": "other"})
    assert r.returncode == 2 and "unknown label" in r.stderr
    r = sh([BACKUP], {"DATABASE_URL": "not-a-url", "BACKUP_LABEL": "enterprise"})
    assert (
        r.returncode == 2 and "not-a-url" not in r.stderr + r.stdout
    )  # URL e pavlefshme s'printohet


def test_restore_refuses_missing_checksum_mismatch_and_bad_names(tmp_path):
    dump = tmp_path / "sms-enterprise-x.dump"
    dump.write_bytes(b"not a real dump")
    env = {"ADMIN_URL": "postgresql://u:secretpw@127.0.0.1:1/postgres"}
    r = sh([RESTORE, dump, "sms_new"], env)
    assert r.returncode == 4 and "checksum" in r.stderr  # pa .sha256: refuzim
    (tmp_path / "sms-enterprise-x.dump.sha256").write_text(f"{'0' * 64}  {dump.name}\n")
    r = sh([RESTORE, dump, "sms_new"], env)
    assert r.returncode == 4 and "checksum mismatch" in r.stderr
    for bad in ("Bad-Name", "1abc", "postgres", "template1", "a" * 64):
        assert sh([RESTORE, dump, bad], env).returncode == 2, bad
    assert "secretpw" not in r.stderr + r.stdout


# ------------------------------------------------------------------ me PostgreSQL të përkohshëm
@pytest.fixture(scope="module")
def dbs():
    """Një Enterprise dhe një Central i migruar + i mbushur, të përkohshëm; pastrohen në fund."""
    d = rd.Drill(admin_url().render_as_string(hide_password=False), Path("."))
    out = {}
    try:
        for label in ("enterprise", "central"):
            db = d.name(label, "tst")
            d.create_db(db)
            d.migrate_and_seed(label, db)
            out[label] = db
        yield d, out
    finally:
        d.cleanup()


def backup(d, label, db, bdir, extra=None):
    env = {
        **d.base_env,
        "BACKUP_LABEL": label,
        "BACKUP_DIR": str(bdir),
        "DATABASE_URL": rd._url(d.admin, db, "postgresql"),
        **(extra or {}),
    }
    return sh([BACKUP], env)


def restore(d, dump, target, extra=None):
    return sh([RESTORE, dump, target], {**d.base_env, **(extra or {})})


def dump_of(bdir, label):
    return sorted(Path(bdir).glob(f"sms-{label}-*.dump"))[-1]


@needs_pg
def test_full_drill_restores_both_databases_without_the_source(tmp_path):
    before = _names(rd.Drill(admin_url().render_as_string(hide_password=False), tmp_path))
    r = sh(
        [sys.executable, "-m", "scripts.recovery_drill", "--workdir", tmp_path],
        {"ADMIN_URL": admin_url().render_as_string(hide_password=False)},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (
        "DRILL OK" in r.stdout
        and r.stdout.count("IDENTICAL") == 2
        and "source dropped before restore" in r.stdout
    )
    assert (
        _names(rd.Drill(admin_url().render_as_string(hide_password=False), tmp_path)) == before
    )  # asnjë bazë e mbetur
    assert_no_credential_leak(r.stdout + r.stderr)


def _names(d):
    with d.eng.connect() as c:
        return sorted(
            r[0]
            for r in c.execute(
                text("select datname from pg_database where datname like 'f05drill\\_%'")
            )
        )


@needs_pg
def test_backup_artifacts_contain_no_credentials_and_record_label_and_head(dbs, tmp_path):
    d, db = dbs
    for label in ("enterprise", "central"):
        r = backup(d, label, db[label], tmp_path)
        assert r.returncode == 0, r.stderr
        dump = dump_of(tmp_path, label)
        meta = json.loads(Path(f"{dump}.meta.json").read_text())
        assert (
            meta["label"] == label
            and meta["bytes"] == dump.stat().st_size
            and len(meta["sha256"]) == 64
        )
        assert meta["alembic_revision"] == {"enterprise": "0034", "central": "0031"}[label]
        assert "NOT point-in-time" in meta["kind"]
        blob = (
            r.stdout
            + r.stderr
            + Path(f"{dump}.meta.json").read_text()
            + Path(f"{dump}.sha256").read_text()
        )
        assert_no_credential_leak(blob)
        assert d.admin.host not in Path(f"{dump}.meta.json").read_text()
        assert oct(dump.stat().st_mode & 0o777) == "0o600"  # umask 077


@needs_pg
def test_wrong_label_is_refused_in_both_directions(dbs, tmp_path):
    d, db = dbs
    for label, wrong_db in (("central", db["enterprise"]), ("enterprise", db["central"])):
        r = backup(d, label, wrong_db, tmp_path)
        assert r.returncode == 5 and "refusing" in r.stderr, r.stderr
    assert list(tmp_path.glob("sms-*")) == []  # asnjë artefakt


@needs_pg
def test_backup_fails_nonzero_and_leaves_nothing_when_pg_dump_fails_or_db_unreachable(
    dbs, tmp_path
):
    d, db = dbs
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "pg_dump").write_text(
        "#!/usr/bin/env bash\necho 'simulated pg_dump failure' >&2\nexit 1\n"
    )
    (stubs / "pg_dump").chmod(stubs.stat().st_mode | stat.S_IXUSR)
    out = tmp_path / "out"
    r = backup(d, "enterprise", db["enterprise"], out, {"PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode != 0 and "simulated pg_dump failure" in r.stderr
    assert list(out.glob("sms-*")) == []  # as .partial, as dump të gjysmë
    bad = rd._url(d.admin.set(port=1), db["enterprise"], "postgresql")
    r = sh(
        [BACKUP],
        {**d.base_env, "BACKUP_LABEL": "enterprise", "BACKUP_DIR": str(out), "DATABASE_URL": bad},
    )
    assert r.returncode != 0 and list(out.glob("sms-*")) == []


@needs_pg
def test_restore_round_trip_keeps_enterprise_and_central_apart(dbs, tmp_path):
    d, db = dbs
    for label in ("enterprise", "central"):
        assert backup(d, label, db[label], tmp_path).returncode == 0
    ent, cen = dump_of(tmp_path, "enterprise"), dump_of(tmp_path, "central")
    tgt_e, tgt_c = d.name("enterprise", "rt"), d.name("central", "rt")
    r = restore(d, ent, tgt_c)  # Enterprise te emri Central
    assert r.returncode == 5 and "Central-named" in r.stderr
    r = restore(d, cen, tgt_e)  # Central te emri jo-Central
    assert r.returncode == 5 and "central" in r.stderr
    r = restore(d, ent, tgt_e, {"RESTORE_LABEL": "central"})
    assert r.returncode == 5
    assert _names_like(d, "f05drill_").isdisjoint(
        {tgt_e, tgt_c}
    )  # asnjë bazë e krijuar nga refuzimet
    for dump, tgt, label in ((ent, tgt_e, "enterprise"), (cen, tgt_c, "central")):
        r = restore(d, dump, tgt)
        assert r.returncode == 0, r.stderr
        d.created.append(tgt)
        d.verify(label, tgt)  # kokë migrimesh + ledger
    r = restore(d, ent, tgt_e)  # tashmë ekziston
    assert r.returncode == 3 and "already exists" in r.stderr
    assert d.db_exists(tgt_e)  # nuk u prek


def _names_like(d, prefix):
    with d.eng.connect() as c:
        return {
            r[0]
            for r in c.execute(
                text("select datname from pg_database where datname like :p"), {"p": prefix + "%"}
            )
        }


@needs_pg
def test_corrupted_or_truncated_backups_are_detected(dbs, tmp_path):
    d, db = dbs
    assert backup(d, "enterprise", db["enterprise"], tmp_path).returncode == 0
    dump = dump_of(tmp_path, "enterprise")
    tgt = d.name("enterprise", "bad")
    # 1) bajte të ndryshuara pas checksum-it
    flipped = tmp_path / "sms-enterprise-flipped.dump"
    data = bytearray(dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    flipped.write_bytes(bytes(data))
    (tmp_path / f"{flipped.name}.sha256").write_text(
        Path(f"{dump}.sha256").read_text().replace(dump.name, flipped.name)
    )
    r = restore(d, flipped, tgt)
    assert r.returncode == 4 and "checksum mismatch" in r.stderr
    # 2) i prerë, me checksum të rillogaritur (sulm/gabim që e "ripastron" checksum-in): TOC/lexim e kap
    cut = tmp_path / "sms-enterprise-cut.dump"
    cut.write_bytes(dump.read_bytes()[: dump.stat().st_size // 3])
    subprocess.run(
        f"sha256sum {cut.name} > {cut.name}.sha256", shell=True, cwd=tmp_path, check=True
    )
    r = restore(d, cut, tgt)
    assert r.returncode == 4 and "truncated" in r.stderr
    # 3) pa checksum, pa mbivendosje: refuzim; me mbivendosje të qartë vetëm paralajmërim (dump-i i mirë)
    nosum = tmp_path / "sms-enterprise-nosum.dump"
    shutil.copy(dump, nosum)
    assert restore(d, nosum, tgt).returncode == 4
    assert not d.db_exists(tgt)  # asnjë nga rastet s'la bazë të gjysmë


@needs_pg
def test_failed_restore_drops_only_the_database_it_created(dbs, tmp_path):
    d, db = dbs
    assert backup(d, "enterprise", db["enterprise"], tmp_path).returncode == 0
    dump = dump_of(tmp_path, "enterprise")
    real = shutil.which("pg_restore")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    stub = stubs / "pg_restore"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in --list|--file=*) exec ' + real + ' "$@";; esac; done\n'
        "echo 'simulated restore failure' >&2\nexit 1\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    tgt = d.name("enterprise", "fail")
    r = restore(d, dump, tgt, {"PATH": f"{stubs}:{os.environ['PATH']}"})
    assert r.returncode != 0 and "was dropped" in r.stderr
    assert not d.db_exists(tgt)
    assert d.db_exists(db["enterprise"])  # baza burim e paprekur
    r = restore(d, dump, tgt, {"PATH": f"{stubs}:{os.environ['PATH']}", "KEEP_FAILED_RESTORE": "1"})
    assert r.returncode != 0 and d.db_exists(tgt)  # e ruajtur me kërkesë, për hetim
    d.drop_db(tgt)


@needs_pg
def test_head_and_ledger_verification_fail_on_a_bad_database(dbs, tmp_path):
    d, db = dbs
    tgt_c = d.name("central", "neg")
    assert backup(d, "central", db["central"], tmp_path).returncode == 0
    assert restore(d, dump_of(tmp_path, "central"), tgt_c).returncode == 0
    d.created.append(tgt_c)
    d.verify("central", tgt_c)  # i mirë
    env = d.env_for("central", tgt_c)
    eng = rd.create_engine(env["CENTRAL_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.exec_driver_sql("update money_sequence set last_seq = 0")  # sekuenca prapa ledger-it
    r = sh([sys.executable, "-m", "apps.central.tools.recovery_ledger"], env)
    assert r.returncode == 1 and "money_sequence" in r.stdout
    with eng.connect() as c:
        c.exec_driver_sql("update central_alembic_version set version_num = '0001'")
    r = sh([sys.executable, "-m", "scripts.recovery_verify", "head", "--label", "central"], env)
    assert r.returncode == 1 and "MISMATCH" in r.stdout
    eng.dispose()
    # gishti zbulon cdo ndryshim midis burimit dhe kopjes
    fp_good, fp_bad = tmp_path / "good.json", tmp_path / "bad.json"
    env_src = d.env_for("central", db["central"])
    assert (
        sh(
            [
                sys.executable,
                "-m",
                "scripts.recovery_verify",
                "fingerprint",
                "--label",
                "central",
                "--out",
                fp_good,
            ],
            env_src,
        ).returncode
        == 0
    )
    assert (
        sh(
            [
                sys.executable,
                "-m",
                "scripts.recovery_verify",
                "fingerprint",
                "--label",
                "central",
                "--out",
                fp_bad,
            ],
            env,
        ).returncode
        == 0
    )
    r = sh([sys.executable, "-m", "scripts.recovery_verify", "compare", fp_good, fp_bad])
    assert r.returncode == 1 and "DIFFERENT" in r.stdout
    # URL mungon/e gabuar: gabim përdorimi (2), pa nxjerrë URL
    r = sh(
        [sys.executable, "-m", "scripts.recovery_verify", "head", "--label", "central"],
        {"CENTRAL_DATABASE_URL": ""},
    )
    assert r.returncode == 2


@needs_pg
def test_rotation_only_touches_the_same_label(dbs, tmp_path):
    d, db = dbs
    old = time.time() - 40 * 86400
    keep = []
    for label in ("enterprise", "central"):
        for ext in ("dump", "dump.sha256", "dump.meta.json"):
            f = tmp_path / f"sms-{label}-20200101T000000Z.{ext}"
            f.write_text("x")
            os.utime(f, (old, old))
            if label == "central":
                keep.append(f)
    r = backup(d, "enterprise", db["enterprise"], tmp_path, {"KEEP_DAYS": "14"})
    assert r.returncode == 0, r.stderr
    assert not list(
        tmp_path.glob("sms-enterprise-20200101*")
    )  # i vjetri Enterprise u fshi (të treja skedarët)
    assert all(f.exists() for f in keep)  # Central i paprekur nga rotacioni i Enterprise
    assert dump_of(tmp_path, "enterprise").exists()  # i riu ekziston


# ------------------------------------------------------------------ kufiri Enterprise/Central dhe ledger-i i Central
def _imported_roots(path):
    import ast

    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_scripts_import_nothing_from_apps_and_central_tooling_lives_under_central():
    for f in (ROOT / "scripts").rglob("*.py"):
        bad = {m for m in _imported_roots(f) if m == "apps" or m.startswith("apps.")}
        assert not bad, (f.name, bad)  # kufiri: Enterprise/shared scripts s'importojnë Central
    text = (ROOT / "scripts/recovery_drill.py").read_text()
    assert "e2e.recovery_central_seed" in text and "apps.central.tools.recovery_ledger" in text
    assert (ROOT / "apps/central/tools/recovery_ledger.py").exists()
    assert (ROOT / "e2e/recovery_central_seed.py").exists()
    # mjeti i Central nuk varet nga kodi Enterprise (rregull i inventarit të imazhit Central)
    src = (ROOT / "apps/central/tools/recovery_ledger.py").read_text()
    assert "subprocess" not in src and "import scripts" not in src and "from scripts" not in src


def test_central_ledger_tool_is_read_only_and_fails_closed_without_leaking_the_url(monkeypatch):
    r = sh([sys.executable, "-m", "apps.central.tools.recovery_ledger", "--help"])
    assert r.returncode == 0 and "usage:" in r.stdout
    r = sh(
        [sys.executable, "-m", "apps.central.tools.recovery_ledger", "--url-env", "F05_UNSET_VAR"]
    )
    assert r.returncode == 2 and "not set" in r.stderr
    leaky = "postgresql+psycopg://leakuser:Unique-Secret-9f3k2!@127.0.0.1:1/nodb"
    r = sh(
        [sys.executable, "-m", "apps.central.tools.recovery_ledger", "--url-env", "F05_LEAKY"],
        {"F05_LEAKY": leaky},
    )
    assert r.returncode == 2 and "cannot verify" in r.stderr  # baza e paarritshme
    assert_no_credential_leak(r.stdout + r.stderr, ["Unique-Secret-9f3k2!"])
    src = (ROOT / "apps/central/tools/recovery_ledger.py").read_text()
    assert not re.search(r"\.(add|delete|merge|commit|execute)\(", src)  # vetëm lexim


@pytest.fixture(scope="module")
def leak_role(dbs):
    """Rol i përkohshëm me SEKRET unik dhe të drejtat minimale të nevojshme (CREATEDB + lexim); fshihet në fund."""
    d, _ = dbs
    role = f"f05drill_role_{d.tag}"
    with d.eng.connect() as c:
        c.execute(text(f"create role {role} login createdb password '{SECRET}'"))
        c.execute(text(f"grant pg_read_all_data to {role}"))
    try:
        yield d, d.admin.set(username=role, password=SECRET)
    finally:
        with d.eng.connect() as c:
            c.execute(text(f"drop owned by {role}"))
            c.execute(text(f"drop role if exists {role}"))


@needs_pg
def test_no_credential_leaks_with_a_unique_high_entropy_secret(dbs, leak_role, tmp_path):
    d, url = leak_role
    _, db = dbs
    out, restored = [], []
    try:
        for label, other in (("enterprise", "central"), ("central", "enterprise")):
            env = {
                **d.base_env,
                "BACKUP_LABEL": label,
                "BACKUP_DIR": str(tmp_path),
                "DATABASE_URL": rd._url(url, db[label], "postgresql"),
            }
            r = sh([BACKUP], env)
            assert r.returncode == 0, r.stderr
            dump = dump_of(tmp_path, label)
            out += [
                r.stdout,
                r.stderr,
                Path(f"{dump}.meta.json").read_text(),
                Path(f"{dump}.sha256").read_text(),
            ]
            bad = sh(
                [BACKUP], {**env, "BACKUP_LABEL": other}
            )  # rruga e dështimit: etiketë e gabuar
            assert bad.returncode == 5
            out += [bad.stdout, bad.stderr]
            renv = {**d.base_env, "ADMIN_URL": rd._url(url, "postgres", "postgresql")}
            tgt = d.name(label, "lk")
            r = sh([RESTORE, dump, tgt], renv)
            assert r.returncode == 0, r.stderr
            restored.append(tgt)
            again = sh([RESTORE, dump, tgt], renv)  # rruga e dështimit: target ekzistues
            assert again.returncode == 3
            out += [r.stdout, r.stderr, again.stdout, again.stderr]
            cut = tmp_path / f"sms-{label}-cut.dump"  # rruga e dështimit: checksum i gabuar
            cut.write_bytes(dump.read_bytes())
            Path(f"{cut}.sha256").write_text(f"{'0' * 64}  {cut.name}\n")
            bad = sh([RESTORE, cut, d.name(label, "lk2")], renv)
            assert bad.returncode == 4
            out += [bad.stdout, bad.stderr]
        # drill i plotë me të njëjtin rol/sekret
        r = sh(
            [sys.executable, "-m", "scripts.recovery_drill", "--workdir", tmp_path / "drill"],
            {"ADMIN_URL": url.render_as_string(hide_password=False)},
        )
        assert r.returncode == 0 and "DRILL OK" in r.stdout, r.stdout + r.stderr
        out += [r.stdout, r.stderr]
        blob = "\n".join(out)
        assert (
            "sms-enterprise-" in blob
        )  # fjalë të zakonshme mbeten të paprekura (pa «redaktim» të gjerë)
        assert_no_credential_leak(blob, [SECRET])
    finally:
        for t in restored:
            d.drop_db(t)
