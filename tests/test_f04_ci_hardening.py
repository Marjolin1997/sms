"""M11-I/F-04: invariantet e CI/skanimit të sekreteve/E2E (kontrolle statike të skedarëve; pa rrjet)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def code(block: str) -> str:
    """Pa rreshtat e komentuar (arsyetimet shkruajnë fjalë si 'provider' ose 'fetch-depth')."""
    return "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))


def job(name: str) -> str:
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", CI, re.S | re.M)
    assert m, f"job {name} missing"
    return m.group(1)


def test_permissions_and_concurrency_unchanged():
    assert re.search(r"^permissions:\n  contents: read\n", CI, re.M)
    assert "cancel-in-progress: true" in CI
    assert "write" not in re.search(r"^permissions:\n(.*?)\n\w", CI, re.M | re.S).group(1)


def test_central_e2e_is_blocking_and_isolated():
    j = job("central-e2e")
    assert "continue-on-error" not in code(
        j
    )  # bllokues pas 3 ekzekutimesh të gjelbra (82b1895, 256f282, 4162d99)
    assert "BLLOKUES" in j
    assert "e2e/run_central_e2e.sh" in j
    assert "timeout-minutes:" in j
    # asnjë sekret i repo-s dhe asnjë kredencial provider-i në job
    assert "secrets." not in j
    assert not re.search(r"(TWILIO|SMTP|STRIPE|PAYPAL|PROVIDER)", code(j), re.I)


def test_all_new_jobs_have_realistic_timeouts_and_only_audit_is_advisory():
    expected = {
        "backend-static": 15, "pytest-pg": 60, "pytest-sqlite": 60, "migrations-backup": 30,
        "backend-ci-gate": 5, "e2e": 30, "central-e2e": 30, "docker": 45, "secret-scan": 10,
    }  # fmt: skip
    for name, minutes in expected.items():
        assert f"timeout-minutes: {minutes}" in job(name), name
    for name in expected:
        assert "continue-on-error" not in code(job(name)), name  # të gjitha bllokuese
    assert "continue-on-error: true" in job("audit")  # i vetmi këshillues


def test_failure_artifacts_are_logs_and_screenshots_only():
    for name in ("e2e", "central-e2e"):
        j = job(name)
        assert "if: failure()" in j and "actions/upload-artifact@v4" in j
        assert "retention-days:" in j
        up = j[j.index("upload-artifact") :]
        for forbidden in ("seed.out", ".env", "work", ".db", ".pem", "ids.json"):
            assert forbidden not in up, (name, forbidden)


def test_secret_scan_is_current_tree_only_with_pinned_checked_binary():
    j = job("secret-scan")
    assert "fetch-depth" not in code(j)  # pa histori
    assert "gitleaks dir ." in j and "--redact" in j and "-c .gitleaks.toml" in j
    assert "sha256sum -c" in j and re.search(r"V=\d+\.\d+\.\d+", j)


def test_enterprise_migration_head_assertion_is_not_hardcoded():
    j = job("migrations-backup")
    assert 'alembic heads | wc -l)" = "1"' in j
    assert "alembic current | cut -d' ' -f1)\" = \"$(alembic heads" in j
    # kokat e ngulitura të migrimeve nuk shtohen për Enterprise në CI (Central ka gate-in e vet 0031)
    assert "0034" not in j


def test_gitleaks_config_has_narrow_allowlist_only():
    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text())
    assert cfg["extend"]["useDefault"] is True
    allow = cfg["allowlist"]
    assert "paths" not in allow  # asnjë përjashtim i përgjithshëm rruge
    for r in allow["regexes"]:
        assert r.startswith(("wV0d", "^GEZD", "^bench-", "^Ed25519")), r  # vetëm vlera të emërtuara
    for rule in cfg.get("rules", []):
        for p in rule.get("allowlist", {}).get("paths", []):
            assert not re.search(r"(^|\^)(tests|docs|\.github|e2e|app|apps)/?(\$|\.\*)", p), p
            assert p.startswith("^tests/") and p.endswith("$")  # skedar i vetëm i emërtuar
    text = (ROOT / ".gitleaks.toml").read_text()
    assert (
        "wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ=" in text
    )  # çelësi sintetik i njohur, jo prodhim


def test_gitignore_and_examples():
    gi = (ROOT / ".gitignore").read_text().splitlines()
    for line in ("*.pem", ".env.*", "!.env.example", "!.env.central.example"):
        assert line in gi
    assert gi.index(".env.*") < gi.index("!.env.example")  # përjashtimi pas rregullit
    assert (ROOT / ".env.example").exists() and (ROOT / ".env.central.example").exists()


def test_dependabot_covers_frontend_central():
    d = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "directory: /frontend-central" in d and "directory: /frontend\n" in d


def test_e2e_scripts_do_not_hardcode_the_playwright_browser_path():
    for f in ("test_console.py", "test_central_console.py", "test_central_audit.py"):
        src = (ROOT / "e2e" / f).read_text()
        assert "/opt/pw-browsers" not in src, f
        assert "from e2e.browser_util import" in src and "dump_failure(" in src, f
    util = (ROOT / "e2e" / "browser_util.py").read_text()
    assert "/opt/pw-browsers" not in util and "E2E_CHROME" in util


def test_central_e2e_runner_is_synthetic_local_only():
    sh = (ROOT / "e2e" / "run_central_e2e.sh").read_text()
    assert "set -euo pipefail" in sh
    assert 'CENTRAL_DATABASE_URL="sqlite:///' in sh
    assert "CENTRAL_ENV=development" in sh
    assert "violations(os.environ)" in sh  # roja "pa ofrues të vërtetë"
    assert re.search(r"secrets\.token_urlsafe", sh)  # kredenciale të gjeneruara, jo të ngulitura
    assert not re.search(r"(postgresql|https?://(?!127\.0\.0\.1))", sh)
    assert (ROOT / "e2e" / "run_central_e2e.sh").stat().st_mode & 0o111


# --- M11-I/F-04 performancë: shardim, paralelizim, porta përfundimtare -----------------------------------


def test_pytest_is_sharded_deterministically_with_isolated_databases():
    pg, lite = job("pytest-pg"), job("pytest-sqlite")
    assert re.search(r"shard: \[0, 1, 2, 3\]", pg) and "--shard-count 4" in pg
    assert re.search(r"shard: \[0, 1\]", lite) and "--shard-count 2" in lite
    for j in (pg, lite):
        # `python -m pytest` (cwd në sys.path): `pytest` i zhveshur s'e gjen `-p tests.ci_shard`
        assert (
            "python -m pytest -q -p tests.ci_shard" in j
            and "--shard-index ${{ matrix.shard }}" in j
        )
        assert "fail-fast: false" in j  # çdo shard raporton veçmas
        assert "needs:" not in code(j)  # paralel: s'presin jobe të tjera
    assert "services:" in pg and "image: postgres:16" in pg  # çdo shard: PG i vet
    assert "SMS_TEST_DATABASE_URL" in pg
    assert (
        "SMS_TEST_DATABASE_URL" not in code(lite) and "services:" not in lite
    )  # SQLite i përkohshëm


def test_migrations_backup_runs_in_its_own_job_not_with_pytest():
    m = job("migrations-backup")
    for needle in ("alembic downgrade base", "apps/central/alembic.ini", "scripts/backup.sh",
                   "scripts/restore.sh", "scripts.verify_ledger", "image: postgres:16"):  # fmt: skip
        assert needle in m, needle
    for name in ("pytest-pg", "pytest-sqlite", "backend-static"):
        assert "alembic" not in job(name) and "backup.sh" not in job(name), name


def test_gate_collects_every_backend_job_and_independent_jobs_are_parallel():
    g = job("backend-ci-gate")
    assert re.search(r"needs: \[backend-static, pytest-pg, pytest-sqlite, migrations-backup\]", g)
    assert "always()" in g and "needs.*.result" in g
    assert "needs:" not in code(job("docker"))  # Docker është i pavarur
    assert re.search(r"needs: \[frontend\]", job("e2e"))  # E2E Enterprise: vetëm frontend
    assert "backend" not in re.search(r"needs: (\[.*?\])", job("e2e")).group(1)
    assert "needs:" not in code(job("backend-static"))


def test_backend_static_proves_the_shard_partition_in_ci():
    j = job("backend-static")
    assert "scripts.ci_shard_proof --mode pg --shards 4" in j
    assert "scripts.ci_shard_proof --mode sqlite --shards 2" in j
    assert "ruff check ." in j and "ruff format --check ." in j and "bandit" in j
