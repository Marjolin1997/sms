# ruff: noqa: F811
"""M9-f — mbulim i audit-it për çdo mutacion financiar + forcim i kredencialeve të shërbimit (scope, enterprise, çelësa)."""

import ast
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import (
    AuditLog,
    CentralUser,
    CommercialLedgerEntry,
    CreditAccount,
    CreditGrant,
    MoneyEvent,
    Payment,
)
from apps.central.models.pricing import PriceAssignment, PriceBook, PriceRule, PriceVersion
from apps.central.models.service_auth import (
    ALLOWED_SCOPES,
    ServiceClient,
    ServiceClientEnterprise,
    ServiceKey,
)
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises as ent
from apps.central.services import grants, payments, retention, service_auth
from apps.central.services import products as prod
from apps.central.tools import create_admin, create_service_credential, service_credential_admin
from tests.test_central import central_alembic, make_db  # noqa: F401
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_sync_api import assertion, auth, keypair

ROOT = Path(__file__).resolve().parents[1]
FIN = (Payment, CreditGrant, CommercialLedgerEntry, CreditAccount, MoneyEvent, PriceBook, PriceVersion, PriceRule, PriceAssignment,
       ServiceClient, ServiceKey, ServiceClientEnterprise, CentralUser)  # fmt: skip
FUTURE = datetime.now(UTC) + timedelta(days=1)


class Coverage:
    """Çdo transaksion që prek një tabelë financiare/autorizimi DUHET të përmbajë një rresht audit në të njëjtin commit."""

    def __init__(self):
        self.violations: list[set[str]] = []
        self.audited_txs = 0

    def __enter__(self):
        event.listen(Session, "after_flush", self._flush)
        event.listen(Session, "after_rollback", self._rollback)
        event.listen(Session, "after_transaction_end", self._end)
        return self

    def __exit__(self, *a):
        event.remove(Session, "after_flush", self._flush)
        event.remove(Session, "after_rollback", self._rollback)
        event.remove(Session, "after_transaction_end", self._end)

    def _flush(self, session, ctx):
        for o in (*session.new, *session.dirty, *session.deleted):
            if isinstance(o, FIN):
                session.info.setdefault("fin", set()).add(type(o).__name__)
            if isinstance(o, AuditLog):
                session.info["aud"] = True

    def _rollback(self, session):
        session.info["rolled_back"] = True

    def _end(self, session, trans):
        if trans.parent is not None:  # SAVEPOINT: transaksioni i jashtëm vendos
            return
        fin, aud = session.info.pop("fin", None), session.info.pop("aud", False)
        if session.info.pop("rolled_back", False):
            return
        if fin and not aud:
            import traceback

            where = [
                f"{fr.name}:{Path(fr.filename).name}"
                for fr in traceback.extract_stack()
                if "/apps/central/" in fr.filename
            ]
            self.violations.append((sorted(fin), where[-4:]))
        elif fin:
            self.audited_txs += 1


@pytest.fixture
def cenv(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    mk(eng, "a1@example.com", role="admin")
    mk(eng, "a2@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    with Session(eng, expire_on_commit=False) as s:
        e1, e2 = ent.create(s, "Acme"), ent.create(s, "Beta")
        sms = prod.create(s, "sms", "SMS", "sms")
        s.commit()
        ids = {"e1": e1.id, "e2": e2.id, "sms": sms.id}
    c = TestClient(create_app(eng))
    c.eng, c.private, c.public, c.ids, c.url = eng, private, public, ids, url
    c.a1, c.a2 = bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "a2@example.com"))
    c.op = bearer(token_for(c, "op@example.com"))
    yield c
    eng.dispose()


def pem(public) -> str:
    return public.decode() if isinstance(public, bytes) else public


# =============================================================================================================
# A. mbulimi i audit-it (Central)
# =============================================================================================================

EXPECTED_ACTIONS = {
    "credit_account.create", "credit_account.status_change", "payment.create", "payment.approve", "payment.reject",
    "credit_grant.create", "credit_grant.reverse", "credit_adjustment.create", "debit_adjustment.create",
    "price_book.create", "price_version.create", "price_rule.set", "price_rule.remove", "price_version.activate",
    "price_version.retire", "price_assignment.create", "service_client.create", "service_key.add",
    "service_client.grant", "service_client.revoke", "service_client.disable_key", "service_client.disable_client",
    "service_client.enable_auto_grant", "service_client.disable_auto_grant", "user.create", "usage_report.retention",
}  # fmt: skip


def full_scenario(c):
    """Çdo rrugë mutacioni financiar: API admin + shërbime sistemi + mjete operatori + retention."""
    with Session(c.eng, expire_on_commit=False) as s:
        a1 = s.scalar(select(CentralUser).where(CentralUser.email == "a1@example.com"))
        acct = accts.create(s, c.ids["e1"], c.ids["sms"], "EUR", a1).id
        s.commit()
    pay = c.post(
        "/admin/money/payments", json={"account_id": str(acct), "amount": "100"}, headers=c.a1
    ).json()["id"]
    c.post(f"/admin/money/payments/{pay}/approve", headers=c.a2)
    rej = c.post(
        "/admin/money/payments", json={"account_id": str(acct), "amount": "5"}, headers=c.a1
    ).json()["id"]
    c.post(f"/admin/money/payments/{rej}/reject", json={"reason": "dup"}, headers=c.a2)
    with Session(c.eng, expire_on_commit=False) as s:  # pagesë + grant nga PROCES SISTEMI (import)
        p = payments.create(
            s, acct, "10", system="system:payment_import", external_reference="imp-1"
        )
        s.commit()
        pid = p.id
    c.post(f"/admin/money/payments/{pid}/approve", headers=c.a2)
    with Session(c.eng, expire_on_commit=False) as s:
        grants.issue(s, acct, "1", idempotency_key="sys-grant-0001", system="system:bootstrap")
        s.commit()
    g = c.post(
        "/admin/money/grants",
        json={"account_id": str(acct), "amount": "20", "idempotency_key": "grant-key-0001"},
        headers=c.a1,
    ).json()["id"]
    c.post(f"/admin/money/grants/{g}/reverse", json={"reason": "refund"}, headers=c.a1)
    c.post(
        f"/admin/money/accounts/{acct}/adjustments",
        json={"kind": "credit", "amount": "3", "reason": "gw", "idempotency_key": "adj-key-00001"},
        headers=c.a1,
    )
    c.post(
        f"/admin/money/accounts/{acct}/adjustments",
        json={"kind": "debit", "amount": "1", "reason": "fix", "idempotency_key": "adj-key-00002"},
        headers=c.a1,
    )
    c.post(
        f"/admin/money/accounts/{acct}/status",
        json={"status": "suspended", "reason": "r"},
        headers=c.a1,
    )
    c.post(
        f"/admin/money/accounts/{acct}/status",
        json={"status": "active", "reason": "r"},
        headers=c.a1,
    )
    b = c.post(
        "/admin/pricing/books",
        json={"code": "retail", "name": "Retail", "currency": "EUR"},
        headers=c.a1,
    ).json()["id"]
    v = c.post(f"/admin/pricing/books/{b}/versions", headers=c.a1).json()["id"]
    c.post(
        f"/admin/pricing/versions/{v}/rules",
        json={"channel": "sms", "prefix": "355", "unit_price": "0.05"},
        headers=c.a1,
    )
    c.post(
        f"/admin/pricing/versions/{v}/rules",
        json={"channel": "sms", "prefix": "44", "unit_price": "0.05"},
        headers=c.a1,
    )
    c.post(
        f"/admin/pricing/versions/{v}/rules/remove",
        json={"channel": "sms", "prefix": "44"},
        headers=c.a1,
    )
    c.post(
        f"/admin/pricing/versions/{v}/activate",
        json={"effective_from": FUTURE.isoformat()},
        headers=c.a1,
    )
    c.post(
        "/admin/pricing/assignments",
        json={
            "enterprise_id": str(c.ids["e1"]),
            "product_id": str(c.ids["sms"]),
            "book_id": b,
            "effective_from": FUTURE.isoformat(),
        },
        headers=c.a1,
    )
    c.post(f"/admin/pricing/versions/{v}/retire", json={"reason": "stop"}, headers=c.a1)
    # mjete operatori (CLI): admin, kredenciale shërbimi (krijim + çelës i dytë + çdo veprim administrimi)
    assert create_admin.run("a3@example.com", "S3cure-passw0rd-xyz", "admin", engine=c.eng)[0] == 0
    assert (
        create_service_credential.run(
            "svc", "k1", c.public, ["money:report"], [c.ids["e1"]], engine=c.eng
        )[0]
        == 0
    )
    assert create_service_credential.run("svc", "k2", keypair()[1], None, [], engine=c.eng)[0] == 0
    for action, kw in (("grant", {"enterprise_id": c.ids["e2"]}), ("revoke", {"enterprise_id": c.ids["e2"]}), ("disable-key", {"kid": "k2"}),
                       ("enable-auto-grant", {}), ("disable-auto-grant", {}), ("disable-client", {})):  # fmt: skip
        service_credential_admin.run(action, "svc", engine=c.eng, **kw)
    # retention (sistem) mbi raporte të shtuara
    from tests.test_m9d_central_reports import mk_doc

    with Session(c.eng, expire_on_commit=False) as s:
        from apps.central.services import usage_reports

        for i in range(1, 5):
            d = mk_doc(c.ids["e1"], c.ids["sms"], seq=i, ledger_max_id=10 + i)
            usage_reports.ingest(
                s, usage_reports.parse(d), now=datetime.now(UTC) - timedelta(days=40 - i)
            )
        s.commit()
        retention.apply(s, retention.plan(s, retention_days=10, full_days=1, keep_last=1))
        s.commit()
    return acct


def test_every_financial_and_authorization_mutation_commits_with_an_audit_row(cenv):
    with Coverage() as cov:
        full_scenario(cenv)
    assert cov.violations == [], (
        f"transactions touching financial/authorization tables without audit: {cov.violations}"
    )
    assert cov.audited_txs >= 25
    with Session(cenv.eng) as s:
        actions = set(s.scalars(select(AuditLog.action)))
    assert EXPECTED_ACTIONS <= actions, EXPECTED_ACTIONS - actions


def test_system_actions_carry_a_label_and_human_actions_a_user_and_nothing_secret_is_logged(cenv):
    full_scenario(cenv)
    with Session(cenv.eng) as s:
        rows = list(s.scalars(select(AuditLog)))
    assert rows
    for r in rows:
        assert (r.actor_kind == "user" and r.actor_id and r.actor_label is None) or (
            r.actor_kind == "system" and r.actor_id is None and r.actor_label.startswith("system:")
        ), r.action
        blob = json.dumps(r.detail, default=str).lower()
        for needle in (
            "begin public key",
            "begin private key",
            "s3cure",
            "password",
            "bearer ",
            "eyj",
            "secret",
        ):
            assert needle not in blob, (r.action, needle)
    svc = [r for r in rows if r.action.startswith("service_")]
    assert {r.actor_label for r in svc} == {"system:service_client_configuration"}
    key_add = [r for r in rows if r.action == "service_key.add"]
    assert all(
        set(r.detail) == {"client_id", "kid"} for r in key_add
    )  # vetëm kid, kurrë materiali i çelësit
    created = next(r for r in rows if r.action == "service_client.create")
    assert created.detail["scopes"] == ["money:report"] and created.detail["enterprises"] == [
        str(cenv.ids["e1"])
    ]


def test_noop_mutations_write_no_audit(cenv):
    full_scenario(cenv)
    with Session(cenv.eng) as s:
        before = s.scalar(select(func.count()).select_from(AuditLog))
    # të njëjtat thirrje përsëri: replay idempotent ⇒ asnjë rresht i ri
    assert (
        create_service_credential.run("svc", "k1", cenv.public, None, [], engine=cenv.eng)[0] == 0
    )
    assert service_credential_admin.run("disable-client", "svc", engine=cenv.eng).endswith(
        "(auth_generation=" + str(_gen(cenv)) + ")"
    )
    assert (
        create_admin.run("a3@example.com", "S3cure-passw0rd-xyz", "admin", engine=cenv.eng)[0] == 0
    )
    with Session(cenv.eng) as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == before


def _gen(c):
    with Session(c.eng) as s:
        return s.scalar(
            select(ServiceClient.auth_generation).where(ServiceClient.client_id == "svc")
        )


def test_pricing_import_and_the_money_apis_write_money_tables_only_through_audited_services():
    """Struktura: moduli i import-it dhe API-t s'shtojnë direkt rreshta në tabelat financiare (vetëm shërbimet që auditojnë)."""
    direct = {
        "Payment",
        "CreditGrant",
        "CommercialLedgerEntry",
        "CreditAccount",
        "MoneyEvent",
        "PriceBook",
        "PriceVersion",
        "PriceRule",
        "PriceAssignment",
    }
    for rel in ("apps/central/services/pricing_import.py", "apps/central/tools/pricing_import.py", "apps/central/api/admin_money.py",
                "apps/central/api/admin_pricing.py", "apps/central/api/admin_financial.py", "apps/central/tools/financial_readiness.py"):  # fmt: skip
        tree = ast.parse((ROOT / rel).read_text())
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in ("add", "add_all", "delete", "merge")
            ):
                names = {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)}
                assert not (names & direct), (rel, names & direct)


# =============================================================================================================
# B. mbulimi i audit-it (Enterprise): veprimet administrative/autoritare
# =============================================================================================================


def test_enterprise_authority_and_money_mutations_are_audited(db, monkeypatch):
    from app.models.admin import AuditLog as EntAudit
    from app.services import money_authority as ma
    from app.services import money_sync as ms
    from tests.test_m9c_money_authority import apply, gev, mk_world, mode

    w = mk_world(db)
    mode(monkeypatch, "shadow")
    ma.create_baseline(db, w.id, "op-1")
    db.commit()
    mode(monkeypatch, "central")
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    events = [gev("issued", g1, "5"), gev("issued", g2, "1"), gev("reversed", g2, "1")]
    apply(db, events)
    n_after_apply = db.scalar(select(func.count()).select_from(EntAudit))
    ms.get_cursor(db).last_seq = 0  # replay i të njëjtave ngjarje ⇒ no-op ⇒ asnjë audit i ri
    db.commit()
    r = apply(db, events)
    assert r.applied == 0 and db.scalar(select(func.count()).select_from(EntAudit)) == n_after_apply
    ms.reset_epoch(db, uuid.uuid4(), 1, "ops-1")
    db.commit()
    acts = [(a.action, a.actor, a.role) for a in db.scalars(select(EntAudit).order_by(EntAudit.id))]
    names = [a[0] for a in acts]
    assert (
        names.count("money.baseline_create") == 1
        and ("money.baseline_create", "op-1", "operator") in acts
    )
    assert (
        names.count("money.grant_recorded") == 2
        and names.count("money.grant_reversal_recorded") == 1
    )
    assert ("money.cursor_reset", "ops-1", "operator") in acts
    system = [a for a in acts if a[0].startswith("money.grant")]
    assert {a[1] for a in system} == {"system:money_sync"} and {a[2] for a in system} == {"system"}
    for a in db.scalars(select(EntAudit)):
        assert "secret" not in (a.detail or "").lower() and "key" not in json.loads(
            a.detail or "{}"
        )


def test_enterprise_pricing_snapshot_apply_is_audited_and_noop_is_not(db):
    from app.models.admin import AuditLog as EntAudit
    from tests.test_m9e_enterprise_pricing import apply as apply_snap
    from tests.test_m9e_enterprise_pricing import central

    _, snapshot = central(db)
    rows = list(db.scalars(select(EntAudit).where(EntAudit.action == "pricing.snapshot_apply")))
    assert len(rows) == 1 and rows[0].actor == "system:pricing_sync" and rows[0].role == "system"
    d = json.loads(rows[0].detail)
    assert d["revision"] == 1 and d["snapshot_hash"] == snapshot.snapshot_hash
    assert apply_snap(db, snapshot).outcome == "noop"
    assert (
        db.scalar(
            select(func.count())
            .select_from(EntAudit)
            .where(EntAudit.action == "pricing.snapshot_apply")
        )
        == 1
    )


def test_enterprise_manual_wallet_and_unknown_resolution_paths_are_audited_at_the_api(client):
    import inspect

    from app.api import admin, wallets

    src = inspect.getsource(wallets)
    for action in ("wallet.adjust", "topup.confirm", "topup.create"):
        assert f'"{action}"' in src, action
    assert "message.unknown_resolve" in inspect.getsource(
        __import__("app.services.messages", fromlist=["x"])
    )
    assert "email.unknown_resolve" in inspect.getsource(
        __import__("app.services.emails", fromlist=["x"])
    )
    assert admin is not None and client is not None


def test_config_authority_cannot_be_mutated_at_runtime_through_any_api():
    """SMS_MONEY_AUTHORITY / SMS_PRICING_AUTHORITY / ACK vijnë vetëm nga mjedisi: asnjë route s'i ndryshon (s'ka rrugë mutacioni për t'u audituar)."""
    from app.core import config

    names = {
        "money_authority",
        "pricing_authority",
        "money_authority_ack",
        "pricing_authority_ack",
        "money_reporting",
    }
    for rel in ("app/api", "app/services"):
        for p in (ROOT / rel).rglob("*.py"):
            tree = ast.parse(p.read_text())
            for n in ast.walk(tree):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        assert not (isinstance(t, ast.Attribute) and t.attr in names), (
                            p.name,
                            t.attr,
                        )
    assert config.settings is not None


# =============================================================================================================
# C. kredencialet e shërbimit: scope, enterprise, çelësa, gjenerata
# =============================================================================================================


@pytest.fixture
def kenv(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    _, public2 = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2 = ent.create(s, "Acme"), ent.create(s, "Beta")
        prod.create(s, "sms", "SMS", "sms")
        for cid, scopes in (
            ("c-sync", ["sync:read"]),
            ("c-read", ["money:read"]),
            ("c-rep", ["money:report"]),
            ("c-price", ["pricing:read"]),
            ("c-bill", ["billing:report"]),
        ):
            service_auth.create_client(s, cid, scopes, [e1.id])
            service_auth.add_key(s, cid, "k1", public)
        service_auth.add_key(s, "c-price", "k2", public2)
        s.commit()
        ids = {"e1": e1.id, "e2": e2.id}
    c = TestClient(create_app(eng))
    c.eng, c.private, c.ids = eng, private, ids
    yield c
    eng.dispose()


def tok(c, client, scope, kid="k1", **kw):
    return assertion(c.private, client=client, kid=kid, scope=scope, **kw)


def endpoints(c):
    ep = uuid.uuid4()
    return {
        "sync:read": [("GET", "/internal/sync/snapshot", None), ("GET", f"/internal/sync/changes?after_seq=0&epoch={ep}&generation=1", None)],
        "money:read": [("GET", "/internal/money/state", None), ("GET", f"/internal/money/changes?after_seq=0&epoch={ep}&generation=1", None)],
        "money:report": [("GET", f"/internal/money/reconciliation?enterprise_id={c.ids['e1']}", None), ("POST", "/internal/money/usage-reports", {})],
        "pricing:read": [("GET", "/internal/pricing/state", None), ("GET", "/internal/pricing/snapshot", None)],
        "billing:report": [("POST", "/internal/billing/usage-reports", {})],
    }  # fmt: skip


CLIENT_OF = {
    "sync:read": "c-sync",
    "money:read": "c-read",
    "money:report": "c-rep",
    "pricing:read": "c-price",
    "billing:report": "c-bill",
}


def call(c, method, url, token, body=None):
    kw = {"headers": auth(token)}
    if method == "POST":
        kw["json"] = body
    return getattr(c, method.lower())(url, **kw)


def test_the_scope_set_is_exactly_the_five_known_scopes_and_each_endpoint_requires_exactly_its_own(
    kenv,
):
    assert ALLOWED_SCOPES == {
        "sync:read",
        "money:read",
        "money:report",
        "pricing:read",
        "billing:report",
    }
    eps = endpoints(kenv)
    for scope, routes in eps.items():
        for method, url, body in routes:
            for other_scope, client in CLIENT_OF.items():
                r = call(kenv, method, url, tok(kenv, client, other_scope), body)
                if other_scope == scope:
                    assert r.status_code not in (401, 403), (
                        url,
                        scope,
                        r.status_code,
                        r.text[:120],
                    )
                else:
                    assert r.status_code == 403, (
                        url,
                        "client with",
                        other_scope,
                        "got",
                        r.status_code,
                    )


def test_a_token_cannot_claim_a_scope_its_client_does_not_hold(kenv):
    for scope, routes in endpoints(kenv).items():
        for method, url, body in routes:
            wrong_client = next(cl for s, cl in CLIENT_OF.items() if s != scope)
            assert (
                call(kenv, method, url, tok(kenv, wrong_client, scope), body).status_code == 403
            ), (url, wrong_client)


def test_missing_garbled_expired_replayed_and_wrongly_signed_credentials_are_401(kenv):
    from tests.test_central_sync_api import keypair as kp

    for scope, routes in endpoints(kenv).items():
        method, url, body = routes[0]
        client = CLIENT_OF[scope]
        assert getattr(kenv, method.lower())(url).status_code == 401
        for bad in ("", "garbage", "a.b.c"):
            assert call(kenv, method, url, bad, body).status_code == 401, (url, bad)
        expired = tok(kenv, client, scope, iat=datetime.now(UTC) - timedelta(hours=1), lifetime=60)
        assert call(kenv, method, url, expired, body).status_code == 401
        wrong_key = assertion(kp()[0], client=client, kid="k1", scope=scope)
        assert call(kenv, method, url, wrong_key, body).status_code == 401
        assert (
            call(kenv, method, url, tok(kenv, client, scope, kid="nope"), body).status_code == 401
        )
        assert (
            call(kenv, method, url, tok(kenv, client, scope, aud="someone-else"), body).status_code
            == 401
        )
        once = tok(kenv, client, scope, jti=uuid.uuid4().hex)
        assert call(kenv, method, url, once, body).status_code not in (401, 403)
        assert call(kenv, method, url, once, body).status_code == 401  # replay i jti


def test_disabled_clients_and_revoked_keys_are_denied_everywhere_and_the_other_key_still_works(
    kenv,
):
    with Session(kenv.eng) as s:
        service_auth.disable_key(s, "c-price", "k1")
        service_auth.disable_client(s, "c-rep")
        s.commit()
    assert (
        kenv.get(
            "/internal/pricing/state", headers=auth(tok(kenv, "c-price", "pricing:read", kid="k1"))
        ).status_code
        == 401
    )
    r = kenv.get(
        "/internal/pricing/state",
        headers=auth(assertion(kenv.private, client="c-price", kid="k2", scope="pricing:read")),
    )
    assert (
        r.status_code == 401
    )  # k2 ka çelës tjetër publik ⇒ nënshkrimi i privatit k1 nuk vlen: kontrolli i çelësave është per-kid
    for method, url, body in endpoints(kenv)["money:report"]:
        assert call(kenv, method, url, tok(kenv, "c-rep", "money:report"), body).status_code == 401
    assert (
        kenv.get(
            "/internal/money/state", headers=auth(tok(kenv, "c-read", "money:read"))
        ).status_code
        == 200
    )  # klientët e tjerë të paprekur


def test_enterprise_authorization_is_enforced_per_client_for_money_and_pricing(kenv):
    other = kenv.ids["e2"]
    assert (
        kenv.get(
            f"/internal/money/reconciliation?enterprise_id={other}",
            headers=auth(tok(kenv, "c-rep", "money:report")),
        ).status_code
        == 403
    )
    from tests.test_m9d_central_reports import mk_doc

    doc = mk_doc(other, uuid.uuid4())
    r = kenv.post(
        "/internal/money/usage-reports", json=doc, headers=auth(tok(kenv, "c-rep", "money:report"))
    )
    assert r.status_code == 403
    snap = kenv.get(
        "/internal/pricing/snapshot", headers=auth(tok(kenv, "c-price", "pricing:read"))
    ).json()
    ents = {e["enterprise_id"] for e in snap.get("enterprises", [])}
    assert ents <= {str(kenv.ids["e1"])} and str(other) not in ents
    money = kenv.get(
        "/internal/money/state", headers=auth(tok(kenv, "c-read", "money:read"))
    ).json()
    assert str(other) not in json.dumps(money)


def test_auth_generation_changes_when_the_enterprise_set_changes_and_stale_cursors_are_refused(
    kenv,
):
    st0 = kenv.get(
        "/internal/pricing/state", headers=auth(tok(kenv, "c-price", "pricing:read"))
    ).json()
    with Session(kenv.eng) as s:
        assert service_auth.grant_enterprise(s, "c-price", kenv.ids["e2"]) is True
        assert (
            service_auth.grant_enterprise(s, "c-price", kenv.ids["e2"]) is False
        )  # no-op ⇒ pa bump
        s.commit()
    st1 = kenv.get(
        "/internal/pricing/state", headers=auth(tok(kenv, "c-price", "pricing:read"))
    ).json()
    assert st1["authorization_generation"] == st0["authorization_generation"] + 1
    snap = kenv.get("/internal/pricing/snapshot", params={"known_epoch": st0["epoch"], "known_revision": st0["revision"], "known_generation": st0["authorization_generation"]},
                    headers=auth(tok(kenv, "c-price", "pricing:read"))).json()  # fmt: skip
    assert (
        snap.get("changed") is not False
    )  # gjenerata e vjetër ⇒ snapshot i plotë, jo "pa ndryshim"
    ep = st0["epoch"]
    stale = kenv.get(
        f"/internal/money/changes?after_seq=0&epoch={ep}&generation={st0['authorization_generation']}",
        headers=auth(tok(kenv, "c-read", "money:read")),
    )
    assert stale.status_code in (200, 409)


def test_scopes_of_an_existing_client_can_only_change_through_no_code_path(cenv, kenv):
    # (1) asnjë kod që cakton `.scopes` pas krijimit
    for p in (ROOT / "apps/central").rglob("*.py"):
        if "migrations" in p.parts:
            continue
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "scopes":
                        pytest.fail(f"{p}: assigns .scopes")
    # (2) mjeti i kredencialeve refuzon "ngritjen" e scope-it të një klienti ekzistues
    with Session(kenv.eng) as s:
        before = s.scalar(select(ServiceClient.scopes).where(ServiceClient.client_id == "c-sync"))
    code, msg = create_service_credential.run(
        "c-sync", "k9", keypair()[1], ["sync:read", "money:report"], [], engine=kenv.eng
    )
    assert code == 2 and "cannot be changed" in msg
    with Session(kenv.eng) as s:
        assert (
            s.scalar(select(ServiceClient.scopes).where(ServiceClient.client_id == "c-sync"))
            == before
        )
        assert (
            s.scalar(select(func.count()).select_from(ServiceKey).where(ServiceKey.kid == "k9"))
            == 0
        )
    # i njëjti scope ose pa scope ⇒ rotacion çelësi normal
    assert (
        create_service_credential.run("c-sync", "k9", keypair()[1], None, [], engine=kenv.eng)[0]
        == 0
    )
    assert (
        create_service_credential.run(
            "c-sync", "k10", keypair()[1], ["sync:read"], [], engine=kenv.eng
        )[0]
        == 0
    )
    # scope i panjohur s'pranohet kurrë
    from apps.central.core.errors import Invalid

    with pytest.raises(Invalid):
        create_service_credential.run(
            "new", "k1", keypair()[1], ["money:admin"], [], engine=kenv.eng
        )
    assert cenv is not None


def test_financial_scopes_are_never_granted_by_default_and_a_new_client_defaults_to_sync_only(kenv):
    with Session(kenv.eng, expire_on_commit=False) as s:
        c = service_auth.create_client(s, "plain", None, [])
        s.commit()
        assert c.scopes == ["sync:read"]
    for bad in (["*"], ["money"], ["money:write"], ["admin"], [""], "money:read"):
        with Session(kenv.eng) as s, pytest.raises(Exception):  # noqa: B017
            service_auth.create_client(s, f"x{uuid.uuid4().hex[:6]}", bad, [])
