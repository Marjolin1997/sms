"""M2-e: kufijtë përfundimtarë të queue-së (source scan). Përmbledh dhe ngurtëson, pa dublikuar provat e
adapterëve (`test_dispatch_queue.py`, `test_delivery_queue.py`)."""

import ast
import re
from pathlib import Path

from app.services import emails, messages, webhooks

APP = Path(__file__).resolve().parents[1] / "app"

# SKIP LOCKED jashtë app/queue është i lejuar VETËM te këto vende, jashtë abstraksionit qëllimisht
# (M2: campaigns, sweeps, DLR/maintenance nuk janë queue artikujsh). Çdo vend i ri duhet vendim i shprehur.
SKIP_LOCKED_ALLOWLIST = {
    "services/messages.py": 2,  # expire_stale (sweep SENT pa DLR) + recover_stuck (M9-a, SENDING)
    "services/emails.py": 1,  # recover_stuck (M9-a, SENDING i ngecur)
    "services/campaigns.py": 1,  # run_due (lock pune për një campaign)
    "services/payments.py": 1,  # expire_pending (sweep)
    "services/billing_usage.py": 1,  # M9-g2: deliver (lease i raporteve billing; jo rrugë dërgimi)
    "services/sender_request_outbox.py": 1,  # M10-S3: deliver (lease i kërkesave të sender-ave; jo rrugë dërgimi)
    "services/money_usage.py": 1,  # M9-d: deliver (lease i raporteve të përdorimit; jo rrugë dërgimi)
}


def test_skip_locked_exists_only_in_the_queue_package_and_the_documented_exceptions():
    found = {}
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("queue/"):
            continue
        n = len(re.findall(r"skip_locked\s*=\s*True", f.read_text()))
        if n:
            found[rel] = n
    assert found == SKIP_LOCKED_ALLOWLIST


def test_queue_package_depends_on_nothing_but_sqlalchemy_and_the_stdlib():
    stdlib_or_sa = {"enum", "collections", "dataclasses", "datetime", "typing", "sqlalchemy", "app"}
    for f in (APP / "queue").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                top = m.split(".")[0]
                assert top in stdlib_or_sa, (f.name, m)
                if top == "app":
                    assert m.startswith("app.queue"), (
                        f.name,
                        m,
                    )  # nuk importon services/models/providers


def test_domain_services_depend_on_queue_but_never_the_reverse():
    for mod in ("services/messages.py", "services/emails.py", "services/webhook_queue.py"):
        assert "app.queue" in (APP / mod).read_text(), mod


def test_retry_constants_have_a_single_source_feeding_the_specs():
    assert (messages.queue.spec.backoff_s, messages.queue.spec.max_attempts) == (
        messages.BACKOFF_SECONDS, messages.MAX_ATTEMPTS)  # fmt: skip
    assert (emails.queue.spec.backoff_s, emails.queue.spec.max_attempts) == (
        emails.BACKOFF_SECONDS, emails.MAX_ATTEMPTS)  # fmt: skip
    spec = webhooks.queue.spec
    assert (
        list(spec.retry_delays_s) == webhooks.RETRY_DELAYS
        and spec.lease_s == webhooks.LEASE_SECONDS
    )
    assert spec.max_attempts == webhooks.MAX_ATTEMPTS


def test_no_module_outside_the_queue_package_defines_its_own_retry_arithmetic():
    """Backoff `* 2 **` dhe `RETRY_DELAYS[...]` janë mekanikë queue-je: vetëm adapteri i llogarit."""
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("queue/"):
            continue
        t = f.read_text()
        assert (
            "2 ** (attempts" not in t
            and "2 ** (m.attempts" not in t
            and "2 ** (e.attempts" not in t
        ), rel
        assert "RETRY_DELAYS[" not in t, rel
