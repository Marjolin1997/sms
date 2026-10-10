# ruff: noqa: F811
"""M7-f — bootstrap/rakordim i assignment-eve Enterprise↔Product (Enterprise source READ-ONLY, Central target)."""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.providers as providers
from app.core.db import SessionLocal, engine
from app.models.billing import Plan, Subscription
from app.models.email import DomainStatus, Email, EmailDomain
from app.models.enterprise import Enterprise as LocalEnterprise
from app.models.sending import AccountPlan, Route
from app.providers import FakeProvider
from app.services import messages as svc
from app.services import rates, sender_ids
from app.services import wallet as wallets
from app.services.wallet import TopupMethod
from apps.central.models import Enterprise, EnterpriseProduct, Product, SyncOutbox
from apps.central.models.audit import AuditImmutableError, AuditLog
from apps.central.services import audit as audit_svc
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.tools import bootstrap_enterprise_products as bp
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    make_db,
)

PAST = datetime(2020, 1, 1, tzinfo=UTC)
SRC_TABLES = ("sms_enterprises", "sms_account_plans", "sms_sender_ids", "sms_messages",
              "sms_emails", "sms_email_domains", "sms_subscriptions", "sms_plans", "sms_wallets")  # fmt: skip


@pytest.fixture(autouse=True)
def fake_provider():
    providers._registry["fake"] = FakeProvider()


def src_url() -> str:
    return engine.url.render_as_string(hide_password=False)


# --- burimi (Enterprise DB = DB globale e testeve) -------------------------------------------------


class Seed:
    """Ndërton gjendje legacy reale me shërbimet/modelet e Enterprise (jo SQL i shpikur)."""

    def __init__(self, db):
        self.db = db
        self.ids: dict[str, uuid.UUID] = {}
        self._card = None
        self._plan_n = 0

    def _base(self):
        if self._card is None:
            self._card = rates.create_card(self.db, "std", "EUR")
            v = rates.new_draft(self.db, self._card.id)
            rates.set_rate(self.db, v.id, "355", "0.05")
            rates.publish(self.db, v.id, PAST, now=PAST)
            self.db.add(Route(prefix="355", country="AL", provider="fake"))
            self.db.commit()
        return self._card

    def enterprise(self, key: str, status: str = "active") -> uuid.UUID:
        e = LocalEnterprise(
            owner_ref=f"{key}-{uuid.uuid4().hex[:6]}", legal_name=key, status=status
        )
        self.db.add(e)
        self.db.commit()
        self.ids[key] = e.id
        return e.id

    def owner(self, key):
        return self.db.get(LocalEnterprise, self.ids[key]).owner_ref

    def plan(self, key, enabled=True):
        self.db.add(
            AccountPlan(owner_ref=self.owner(key), rate_card_id=self._base().id, enabled=enabled)
        )
        self.db.commit()

    def _sv(self, key):
        return (
            "S" + self.owner(key).split("-")[-1].upper()
        )  # unik për llogari (approved_key UNIQUE)

    def sender(self, key, approved=True):
        o = self.owner(key)
        s = sender_ids.request(self.db, o, "AL", self._sv(key))
        if approved:
            sender_ids.approve(self.db, s.id, "admin")
        self.db.commit()

    def sms_history(self, key):
        """Një mesazh real (përmes `submit`) dhe pastaj sender-i REVOKUAR: mbetet vetëm historia."""
        o = self.owner(key)
        self._base()
        w = wallets.create_wallet(self.db, o, "EUR")
        wallets.confirm_topup(
            self.db, wallets.create_topup(self.db, w.id, "10", TopupMethod.CASH).id
        )
        s = sender_ids.request(self.db, o, "AL", self._sv(key))
        sender_ids.approve(self.db, s.id, "admin")
        self.db.commit()
        svc.submit(self.db, o, "k1", "+355691230003", self._sv(key), text="hi")
        sender_ids.revoke(self.db, s.id, "admin", "test")
        self.db.commit()

    def domain(self, key, name=None, verified=True):
        o = self.owner(key)
        name = name or f"{key.replace('_', '-')}.example.com"
        d = EmailDomain(
            owner_ref=o, domain=name, dkim_selector="s1", dkim_public_key="pub",
            dkim_private_key_enc="enc",
            status=DomainStatus.VERIFIED if verified else DomainStatus.PENDING,
            verified_key=name if verified else None,
        )  # fmt: skip
        self.db.add(d)
        self.db.commit()
        return d

    def email_history(self, key):
        d = self.domain(key, f"hist-{uuid.uuid4().hex[:5]}.example.com", verified=False)
        self.db.add(Email(
            public_id=str(uuid.uuid4()), owner_ref=self.owner(key), idempotency_key="e1",
            request_hash="h", domain_id=d.id, from_email="a@x.com", to_email="b@y.com",
            subject="s", text_body="t", provider="fake",
        ))  # fmt: skip
        self.db.commit()

    def subscription(self, key, included=100, status="ACTIVE"):
        p = Plan(code=f"p{uuid.uuid4().hex[:5]}", name="P", currency="EUR", monthly_fee=Decimal(1),
                 included_emails=included)  # fmt: skip
        self.db.add(p)
        self.db.flush()
        self.db.add(Subscription(owner_ref=self.owner(key), plan_id=p.id, started_at=PAST))
        self.db.commit()
        if status != "ACTIVE":
            self.db.execute(
                text("update sms_subscriptions set status = :s where owner_ref = :o"),
                {"s": status, "o": self.owner(key)},
            )
            self.db.commit()


@pytest.fixture
def seed(db):
    return Seed(db)


# --- Central --------------------------------------------------------------------------------------


@pytest.fixture
def cen(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "head")
    return url


def central_setup(url, seed, keys, products=("sms", "email"), suspended=()):
    eng = create_engine(url)
    with Session(eng, expire_on_commit=False) as s:
        for k in keys:
            e = ent.create(s, k, enterprise_id=seed.ids[k])
            if k in suspended:
                ent.suspend(s, e.id)
        for code in products:
            prod.create(s, code, code.upper(), code)
        s.commit()
    eng.dispose()


def run(cen, **kw):
    return bp.run(src_url(), cen, **kw)


def csnap(url):
    eng = create_engine(url)
    with Session(eng) as s:
        out = {
            "assignments": sorted((str(a.enterprise_id), str(a.product_id), a.status, a.revision)
                                  for a in s.scalars(select(EnterpriseProduct))),
            "outbox": s.scalar(select(func.count()).select_from(SyncOutbox)),
            "audit": s.scalar(select(func.count()).select_from(AuditLog)),
        }  # fmt: skip
    eng.dispose()
    return out


def sdump(db):
    db.rollback()
    return {t: sorted(map(tuple, db.execute(text(f"select * from {t}")).all()), key=repr)
            for t in SRC_TABLES}  # fmt: skip


def assignments(url):
    eng = create_engine(url)
    with Session(eng) as s:
        out = {(str(ep.enterprise_id), p.code): (ep.status, ep.revision, ep.id)
               for ep, p in s.execute(select(EnterpriseProduct, Product).join(
                   Product, Product.id == EnterpriseProduct.product_id))}  # fmt: skip
    eng.dispose()
    return out


def item(rep, eid, code):
    return next(i for i in rep.items if i.enterprise_id == str(eid) and i.product_code == code)


# --- shembuj të plotë ------------------------------------------------------------------------------


def full_world(seed):
    """sms_on: plan+sender, sms_off: plan disabled + sender, sms_rev: plan pa evidencë SMS,
    mail_ok: plan + domen i verifikuar, mail_rev: domen + plan disabled, none: asgjë."""
    for k in ("sms_on", "sms_off", "sms_rev", "mail_ok", "mail_rev", "none"):
        seed.enterprise(k)
    seed.plan("sms_on")
    seed.sender("sms_on")  # noqa: E702
    seed.plan("sms_off", enabled=False)
    seed.sender("sms_off")  # noqa: E702
    seed.plan("sms_rev")
    seed.plan("mail_ok")
    seed.domain("mail_ok")  # noqa: E702
    seed.plan("mail_rev", enabled=False)
    seed.domain("mail_rev")  # noqa: E702
    return list(seed.ids)


# --- klasifikimi: SMS dhe Email ------------------------------------------------------------------------


def test_empty_source(seed, cen):
    central_setup(cen, seed, [])
    rep = run(cen)
    assert rep.ok and rep.scanned == 0 and rep.counts()["create"] == 0 and rep.items == []


def test_sms_confirmed_active_and_suspended_mapping(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    rep = run(cen)
    a, b = item(rep, seed.ids["sms_on"], "sms"), item(rep, seed.ids["sms_off"], "sms")
    assert (a.classification, a.recommended_status, a.action) == ("confirmed", "active", "create")
    assert (b.classification, b.recommended_status, b.action) == (
        "confirmed",
        "suspended",
        "create",
    )


def test_sms_history_alone_is_sms_evidence_and_plan_alone_is_review(seed, cen):
    seed.enterprise("hist")
    seed.plan("hist")
    seed.sms_history("hist")  # sender i revokuar, mesazh ekziston
    seed.enterprise("planonly")
    seed.plan("planonly")
    central_setup(cen, seed, ["hist", "planonly"])
    rep = run(cen)
    h, p = item(rep, seed.ids["hist"], "sms"), item(rep, seed.ids["planonly"], "sms")
    assert (
        h.facts["sms_history"]
        and not h.facts["approved_sender_id"]
        and h.classification == "confirmed"
    )
    assert (
        p.classification == "review" and p.action == "review" and "shared legacy object" in p.reason
    )


def test_no_plan_is_no_evidence_and_no_assignment_is_invented(seed, cen):
    seed.enterprise("bare")
    seed.enterprise("sender_only")
    seed.sender("sender_only")  # sender pa AccountPlan: s'është prova e plotë
    central_setup(cen, seed, ["bare", "sender_only"])
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 0 and rep.counts()["no_evidence"] == 4
    assert assignments(cen) == {}


def test_email_confirmed_requires_verified_domain_and_enabled_plan(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    rep = run(cen)
    ok = item(rep, seed.ids["mail_ok"], "email")
    assert (ok.classification, ok.recommended_status, ok.action) == (
        "confirmed",
        "active",
        "create",
    )
    rv = item(rep, seed.ids["mail_rev"], "email")  # domen i verifikuar + plan disabled = REVIEW
    assert (rv.classification, rv.recommended_status, rv.action) == ("review", None, "review")
    assert "kill-switch" in rv.reason
    assert item(rep, seed.ids["sms_on"], "email").classification == "no_evidence"  # plan ≠ email
    assert item(rep, seed.ids["none"], "email").action == "none"


def test_email_review_from_subscription_or_history_without_verified_domain(seed, cen):
    for k in ("sub", "hist", "pending_domain", "cancelled", "small"):
        seed.enterprise(k)
    seed.plan("sub")
    seed.subscription("sub", included=500)  # noqa: E702
    seed.email_history("hist")
    seed.domain("pending_domain", verified=False)  # domen jo i verifikuar ≠ evidencë
    seed.subscription("cancelled", included=500, status="CANCELLED")
    seed.subscription("small", included=0)
    central_setup(cen, seed, list(seed.ids))
    rep = run(cen)
    assert item(rep, seed.ids["sub"], "email").classification == "review"
    assert item(rep, seed.ids["hist"], "email").classification == "review"
    for k in ("pending_domain", "small"):
        assert item(rep, seed.ids[k], "email").classification == "no_evidence", k
    assert (
        item(rep, seed.ids["cancelled"], "email").classification == "no_evidence"
    )  # sub s'është aktive


# --- planifikim: invalid / conflict / noop / central_only --------------------------------------------


def test_missing_central_enterprise_is_invalid_and_not_created(seed, cen):
    seed.enterprise("a")
    seed.plan("a")
    seed.sender("a")  # noqa: E702
    central_setup(cen, seed, [])
    rep = run(cen, apply=True)
    assert not rep.ok and item(rep, seed.ids["a"], "sms").action == "invalid"
    assert "does not exist in Central" in item(rep, seed.ids["a"], "sms").detail
    eng = create_engine(cen)
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(Enterprise)) == 0  # pa krijim implicit
    assert rep.written == 0


@pytest.mark.parametrize("missing", ["sms", "email", "both"])
def test_missing_product_code_is_a_precondition_and_nothing_is_created(seed, cen, missing):
    seed.enterprise("a")
    seed.plan("a")
    seed.sender("a")
    seed.domain("a")  # noqa: E702
    have = tuple(c for c in ("sms", "email") if missing not in (c, "both"))
    central_setup(cen, seed, ["a"], products=have)
    rep = run(cen, apply=True)
    assert not rep.ok and rep.written == 0 and rep.preconditions
    assert all("not created implicitly" in p or "does not exist" in p for p in rep.preconditions)
    eng = create_engine(cen)
    with Session(eng) as s:
        assert s.scalar(select(func.count()).select_from(Product)) == len(
            have
        )  # Product s'krijohet


def test_existing_matching_assignment_is_noop_and_emits_nothing(seed, cen):
    seed.enterprise("a")
    seed.plan("a")
    seed.sender("a")  # noqa: E702
    central_setup(cen, seed, ["a"])
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        p = s.scalar(select(Product).where(Product.code == "sms"))
        asg.assign_product(s, seed.ids["a"], p.id)
        s.commit()
    before = csnap(cen)
    rep = run(cen, apply=True)
    assert item(rep, seed.ids["a"], "sms").action == "noop" and rep.written == 0
    assert csnap(cen) == before  # zero outbox, zero audit, zero revision bump


def test_conflicting_status_is_reported_never_overwritten_and_blocks_all_writes(seed, cen):
    seed.enterprise("a")
    seed.plan("a", enabled=False)
    seed.sender("a")  # legacy: suspended  # noqa: E702
    seed.enterprise("b")
    seed.plan("b")
    seed.sender("b")  # krijohet normalisht  # noqa: E702
    central_setup(cen, seed, ["a", "b"])
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        p = s.scalar(select(Product).where(Product.code == "sms"))
        asg.assign_product(s, seed.ids["a"], p.id)  # Central: active ≠ legacy suspended
        s.commit()
    before = csnap(cen)
    rep = run(cen, apply=True)
    assert not rep.ok and item(rep, seed.ids["a"], "sms").action == "conflict"
    assert item(rep, seed.ids["b"], "sms").action == "create" and rep.written == 0
    assert csnap(cen) == before  # konflikt ⇒ ZERO shkrime, as për "b"


def test_suspended_central_enterprise_is_a_conflict_not_bypassed(seed, cen):
    seed.enterprise("a")
    seed.plan("a")
    seed.sender("a")  # noqa: E702
    central_setup(cen, seed, ["a"], suspended=("a",))
    rep = run(cen, apply=True)
    assert item(rep, seed.ids["a"], "sms").action == "conflict" and rep.written == 0
    assert assignments(cen) == {}


def test_central_only_assignments_are_reported_and_untouched(seed, cen):
    seed.enterprise("a")
    seed.plan("a")
    seed.sender("a")  # noqa: E702
    seed.enterprise("noev")  # pa evidencë legacy por Central ka assignment
    central_setup(cen, seed, ["a", "noev"])
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        p = s.scalar(select(Product).where(Product.code == "email"))
        asg.assign_product(s, seed.ids["noev"], p.id)
        orphan = ent.create(s, "Only in Central")
        asg.assign_product(s, orphan.id, p.id)
        s.commit()
    before = assignments(cen)
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 1  # vetëm sms i "a"
    reasons = sorted(o["reason"] for o in rep.central_only)
    assert reasons == ["enterprise not in legacy source",
                       "legacy source has no evidence for this product"]  # fmt: skip
    after = assignments(cen)
    assert all(
        after[k] == v for k, v in before.items()
    )  # central_only i paprekur (as status, as rev)


# --- dry-run / apply / idempotencë --------------------------------------------------------------------


def test_dry_run_writes_nothing_and_default_mode_is_dry_run(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    before = csnap(cen)
    rep = run(cen)  # parazgjedhja
    assert not rep.apply and rep.counts()["create"] == 3 and rep.written == 0
    assert csnap(cen) == before
    assert "dry-run (0 writes)" in rep.render()


def test_apply_creates_exact_assignments_with_normal_outbox_and_system_audit(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    ob0 = csnap(cen)["outbox"]
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 3
    a = assignments(cen)
    assert a[(str(seed.ids["sms_on"]), "sms")][:2] == ("active", 1)
    assert a[(str(seed.ids["sms_off"]), "sms")][:2] == ("suspended", 2)  # assign + suspend
    assert a[(str(seed.ids["mail_ok"]), "email")][:2] == ("active", 1)
    assert (str(seed.ids["sms_rev"]), "sms") not in a  # review: s'krijohet vetë
    assert (str(seed.ids["mail_rev"]), "email") not in a
    eng = create_engine(cen)
    with Session(eng) as s:
        evs = list(s.scalars(select(SyncOutbox).order_by(SyncOutbox.seq)))[ob0:]
        assert len(evs) == 4  # 2 aktivë + (active, suspended) për sms_off
        off = [e for e in evs if e.entity_id == a[(str(seed.ids["sms_off"]), "sms")][2]]
        assert [(e.revision, e.payload["status"]) for e in off] == [(1, "active"), (2, "suspended")]
        assert [e.seq for e in evs] == sorted(e.seq for e in evs)
        rows = list(s.scalars(select(AuditLog)))
        assert len(rows) == 3  # një audit për assignment të krijuar (jo për hapin suspend)
        assert {r.actor_kind for r in rows} == {"system"} and {r.actor_id for r in rows} == {None}
        assert {r.actor_label for r in rows} == {"system:enterprise_product_bootstrap"}
        assert {r.action for r in rows} == {"enterprise_product.bootstrap"}
        assert all(
            r.detail["evidence_hash"] and r.detail["classification"] == "confirmed" for r in rows
        )
    eng.dispose()


def test_rerun_is_idempotent_and_source_is_untouched(seed, cen, db):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    src_before = sdump(db)
    run(cen, apply=True)
    mid = csnap(cen)
    rep = run(cen, apply=True)
    assert (
        rep.ok
        and rep.written == 0
        and rep.counts()["create"] == 0
        and rep.counts()["matching"] == 3
    )
    assert csnap(cen) == mid  # zero outbox/audit/revision të reja
    assert sdump(db) == src_before  # Enterprise DB e paprekur (AccountPlan, owner_ref, ...)


def test_source_is_read_only_no_write_statement_and_read_only_transaction(seed, cen, monkeypatch):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    seen: list[str] = []
    real = bp.create_engine

    def spy(url, *a, **k):
        from sqlalchemy import event

        eng = real(url, *a, **k)
        event.listen(eng, "before_cursor_execute", lambda c, cur, stmt, *r: seen.append(stmt))
        return eng

    monkeypatch.setattr(bp, "create_engine", spy)
    run(cen)
    first = seen[0].upper()
    assert "READ ONLY" in first or "QUERY_ONLY" in first
    assert not [
        s
        for s in seen
        if s.strip().split()[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE"}
    ]


# --- audit: skema, semantikë, append-only ---------------------------------------------------------------


def test_audit_check_constraint_and_helper_validation(cen):
    eng = create_engine(cen)
    with Session(eng) as s:
        row = audit_svc.record_system(s, label="system:enterprise_product_bootstrap",
                                      action="x.y", resource_type="t", resource_id="1")  # fmt: skip
        s.commit()
        assert (row.actor_kind, row.actor_id, row.actor_label) == (
            "system",
            None,
            "system:enterprise_product_bootstrap",
        )
        for bad in ("bootstrap", "SYSTEM:x", "system:", "system:" + "a" * 60, "", None):
            with pytest.raises(ValueError):
                audit_svc.record_system(
                    s, label=bad, action="x", resource_type="t", resource_id="1"
                )
        row.detail = {"changed": True}
        with pytest.raises(AuditImmutableError):  # vetëm-shtim mbetet
            s.commit()
        s.rollback()
        # CHECK në nivel DB: kombinime të ndaluara
        for kind, actor, label in (("user", None, None), ("user", None, "system:x"),
                                   ("system", uuid.uuid4(), "system:x"), ("system", None, None),
                                   ("robot", None, "system:x")):  # fmt: skip
            with pytest.raises(IntegrityError):
                s.execute(text(
                    "insert into audit_log (id, actor_kind, actor_id, actor_label, action, resource_type,"
                    " resource_id, created_at) values (:i, :k, :a, :l, 'x', 't', '1', :t)"),
                    {"i": uuid.uuid4().hex if eng.dialect.name == "sqlite" else uuid.uuid4(),
                     "k": kind, "a": None if actor is None else (actor.hex if eng.dialect.name == "sqlite" else actor),
                     "l": label, "t": datetime.now(UTC)})  # fmt: skip
                s.flush()
            s.rollback()
    eng.dispose()


def test_user_audit_still_works_and_migration_marks_existing_rows_as_user(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "0010")
    eng = create_engine(url)
    uid = uuid.uuid4()
    sqlite = eng.dialect.name == "sqlite"
    with eng.begin() as c:
        c.execute(text("insert into users (id, email, password_hash, role, status, created_at, updated_at)"
                       " values (:i, 'a@b.c', 'x', 'admin', 'active', :t, :t)"),
                  {"i": uid.hex if sqlite else uid, "t": datetime.now(UTC)})  # fmt: skip
        c.execute(text("insert into audit_log (id, actor_id, action, resource_type, resource_id, created_at)"
                       " values (:i, :a, 'old', 't', '1', :t)"),
                  {"i": uuid.uuid4().hex if sqlite else uuid.uuid4(), "a": uid.hex if sqlite else uid,
                   "t": datetime.now(UTC)})  # fmt: skip
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        row = c.execute(text("select actor_kind, actor_id, actor_label from audit_log")).one()
    assert row[0] == "user" and row[1] is not None and row[2] is None  # actor_id i pandryshuar
    with Session(eng) as s:
        audit_svc.record_system(s, label="system:enterprise_product_bootstrap", action="a",
                                resource_type="t", resource_id="2")  # fmt: skip
        s.commit()
    with pytest.raises(AssertionError):  # downgrade refuzon kur ka rreshta system
        central_alembic(url, "downgrade", "0010")
    cols = {c["name"]: c for c in inspect(eng).get_columns("audit_log")}
    assert cols["actor_id"]["nullable"] and not cols["actor_kind"]["nullable"]
    fks = inspect(eng).get_foreign_keys("audit_log")
    assert fks and fks[0]["referred_table"] == "users"
    if not sqlite:
        assert fks[0]["options"].get("ondelete", "").upper() == "RESTRICT"
    eng.dispose()


# --- review file ---------------------------------------------------------------------------------------


def review_world(seed, cen):
    seed.enterprise("r")
    seed.plan("r", enabled=False)
    seed.domain("r")  # email review
    seed.enterprise("p")
    seed.plan("p")  # sms review (plan pa evidencë)
    central_setup(cen, seed, ["r", "p"])
    rep = run(cen)
    return rep


def write_review(tmp_path, rows):
    p = tmp_path / "approved.json"
    p.write_text(json.dumps({"version": 1, "approvals": rows}))
    return str(p)


def appr(rep, seed, key, code, status):
    it = item(rep, seed.ids[key], code)
    return {"enterprise_id": str(seed.ids[key]), "product_code": code, "approved_status": status,
            "evidence_hash": it.evidence_hash}  # fmt: skip


def test_review_items_are_never_auto_applied_and_approved_ones_are(seed, cen, tmp_path):
    rep = review_world(seed, cen)
    assert item(rep, seed.ids["r"], "email").action == "review" and rep.ok
    assert run(cen, apply=True).written == 0 and assignments(cen) == {}
    path = write_review(tmp_path, [appr(rep, seed, "r", "email", "suspended"),
                                   appr(rep, seed, "p", "sms", "active")])  # fmt: skip
    rep2 = run(cen, apply=True, approvals=bp.load_review_file(path))
    assert rep2.ok and rep2.written == 2
    a = assignments(cen)
    assert a[(str(seed.ids["r"]), "email")][:2] == ("suspended", 2)
    assert a[(str(seed.ids["p"]), "sms")][:2] == ("active", 1)
    eng = create_engine(cen)
    with Session(eng) as s:
        assert all(r.detail["approved"] is True for r in s.scalars(select(AuditLog)))
    eng.dispose()


def test_stale_review_file_is_rejected_when_evidence_changed(seed, cen, tmp_path, db):
    rep = review_world(seed, cen)
    path = write_review(tmp_path, [appr(rep, seed, "r", "email", "active")])
    # evidenca ndryshon pas përgatitjes së approval-it:
    db.execute(
        text("update sms_account_plans set enabled = :e"), {"e": True}
    )  # plan enabled ⇒ confirmed
    db.commit()
    before = csnap(cen)
    rep2 = run(cen, apply=True, approvals=bp.load_review_file(path))
    assert not rep2.ok and rep2.stale_reviews and rep2.written == 0
    assert csnap(cen) == before  # zero shkrime (as për item-et e tjera të krijueshme)


def test_review_approval_does_not_bypass_central_checks(seed, cen, tmp_path):
    rep = review_world(seed, cen)
    # Central enterprise mungon ⇒ invalid; konflikt ekzistues ⇒ conflict
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        pr = s.scalar(select(Product).where(Product.code == "sms"))
        asg.assign_product(s, seed.ids["p"], pr.id)  # active
        s.commit()
    path = write_review(tmp_path, [appr(rep, seed, "p", "sms", "suspended")])
    rep2 = run(cen, apply=True, approvals=bp.load_review_file(path))
    assert (
        item(rep2, seed.ids["p"], "sms").action == "conflict" and not rep2.ok and rep2.written == 0
    )
    eng.dispose()


@pytest.mark.parametrize(
    "doc",
    [
        "not json", [], {"version": 2, "approvals": []}, {"version": 1}, {"version": 1, "approvals": {}},
        {"version": 1, "approvals": [{"enterprise_id": "x"}]},
        {"version": 1, "approvals": [{"enterprise_id": str(uuid.uuid4()), "product_code": "sms",
                                      "approved_status": "active", "evidence_hash": "zz"}]},
        {"version": 1, "approvals": [{"enterprise_id": str(uuid.uuid4()), "product_code": "voice",
                                      "approved_status": "active", "evidence_hash": "a" * 64}]},
        {"version": 1, "approvals": [{"enterprise_id": str(uuid.uuid4()), "product_code": "sms",
                                      "approved_status": "deleted", "evidence_hash": "a" * 64}]},
        {"version": 1, "approvals": [{"enterprise_id": str(uuid.uuid4()), "product_code": "sms",
                                      "approved_status": "active", "evidence_hash": "a" * 64,
                                      "extra": 1}]},
    ],
)  # fmt: skip
def test_review_file_validation(tmp_path, doc):
    p = tmp_path / "r.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    with pytest.raises(bp.ReviewFileError):
        bp.load_review_file(str(p))


def test_review_file_duplicates_and_unknown_rows_are_rejected(seed, cen, tmp_path):
    rep = review_world(seed, cen)
    row = appr(rep, seed, "r", "email", "active")
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"version": 1, "approvals": [row, row]}))
    with pytest.raises(bp.ReviewFileError, match="duplicate"):
        bp.load_review_file(str(p))
    ghost = {**row, "enterprise_id": str(uuid.uuid4())}
    rep2 = run(cen, apply=True, approvals=[bp.Approval(**ghost)])
    assert not rep2.ok and rep2.stale_reviews and rep2.written == 0


def test_evidence_hash_is_stable_across_runs_and_covers_identity(seed, cen):
    rep1, rep2 = review_world(seed, cen), run(cen)
    assert [i.evidence_hash for i in rep1.items] == [i.evidence_hash for i in rep2.items]
    h = item(rep1, seed.ids["r"], "email").evidence_hash
    assert len(h) == 64 and h != item(rep1, seed.ids["p"], "sms").evidence_hash
    assert h == bp._hash(
        str(seed.ids["r"]), "email", "review", None, item(rep1, seed.ids["r"], "email").facts
    )


# --- raporti, CLI, readiness --------------------------------------------------------------------------


def test_report_is_machine_and_human_readable_and_stable(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    r1, r2 = run(cen), run(cen)
    assert r1.to_dict() == r2.to_dict() and r1.render() == r2.render()  # i qëndrueshëm për rerun
    d = json.loads(json.dumps(r1.to_dict()))
    keys_ = {"enterprise_id", "owner_ref", "product_code", "classification", "recommended_status",
             "reason", "evidence", "evidence_hash", "central_status", "action"}  # fmt: skip
    assert keys_ <= set(d["items"][0]) and {"create", "matching", "conflicts", "invalid", "central_only",
                                            "no_evidence", "sms_confirmed", "email_review"} <= set(d["counts"])  # fmt: skip
    text_ = r1.render()
    assert (
        "CREATE" in text_
        and "REVIEW" in text_
        and "postgresql" not in text_
        and "sqlite" not in text_
    )


def test_cli_exit_codes_and_no_secrets(seed, cen, monkeypatch, capsys, tmp_path):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    monkeypatch.setenv("ENTERPRISE_DATABASE_URL", src_url())
    monkeypatch.setattr(bp.settings, "database_url", cen)
    out = tmp_path / "rep.json"
    assert bp.main(["--format", "json", "--output", str(out)]) == 0  # dry-run: ok
    assert json.loads(out.read_text())["mode"] == "dry-run"
    assert bp.main(["--apply", "--dry-run"]) == 2
    assert bp.main(["--apply"]) == 0
    assert bp.main(["--apply"]) == 0  # idempotent
    monkeypatch.delenv("ENTERPRISE_DATABASE_URL")
    assert bp.main([]) == 2
    monkeypatch.setenv("ENTERPRISE_DATABASE_URL", cen)  # e njëjta URL ⇒ refuzohet
    assert bp.main([]) == 2
    printed = capsys.readouterr()
    assert "password" not in printed.out.lower() + printed.err.lower()


def test_shadow_readiness_report_per_channel_and_unexplained_zero_after_apply(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    rep = run(cen)
    r = rep.readiness
    assert r["projected"] is True and set(r["channels"]) == {"sms", "email"}
    rep = run(cen, apply=True)
    r = rep.readiness
    assert r["projected"] is False
    sms, email = r["channels"]["sms"], r["channels"]["email"]
    assert sms["counts"]["legacy_allow_cp_allow"] == 1  # sms_on
    assert sms["counts"]["legacy_deny_cp_deny"] == 1  # sms_off (suspended)
    assert sms["counts"]["cp_missing"] >= 1  # sms_rev (në review) + të tjerë me plan
    assert email["counts"]["legacy_allow_cp_allow"] == 1  # mail_ok
    assert sms["unexplained"] == 0 and email["unexplained"] == 0  # çdo mospërputhje e shpjeguar
    assert r["unknown_local_enterprise"] == 0 and "withdrawn_local" in r
    assert "Shadow readiness" in rep.render()


def test_readiness_flags_unexplained_mismatch(seed, cen):
    """Central ka assignment email aktiv për një tenant pa evidencë email dhe me plan disabled:
    legacy deny / CP allow pa shpjegim (central_only) ⇒ unexplained = 1."""
    seed.enterprise("x")
    seed.plan("x", enabled=False)
    central_setup(cen, seed, ["x"])
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        p = s.scalar(select(Product).where(Product.code == "email"))
        asg.assign_product(s, seed.ids["x"], p.id)
        s.commit()
    eng.dispose()
    rep = run(cen)
    email, sms = rep.readiness["channels"]["email"], rep.readiness["channels"]["sms"]
    assert email["counts"]["legacy_deny_cp_allow"] == 1 and email["unexplained"] == 1
    assert email["unexplained_items"][0]["class"] == "legacy_deny_cp_allow"
    assert (
        sms["counts"]["cp_missing"] == 1 and sms["unexplained"] == 0
    )  # sms: review (plan pa evidencë)
    assert len(rep.central_only) == 1


# --- kufij ----------------------------------------------------------------------------------------------


def test_tool_has_no_enterprise_writes_deletes_or_enforcement_and_imports_only_central():
    import ast

    src = (ROOT / "apps/central/tools/bootstrap_enterprise_products.py").read_text()
    tree = ast.parse(src)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in imports if m == "app" or m.startswith("app.")]  # Central ↛ Enterprise
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "delete" not in attrs and "AccountPlan" not in names  # pa fshirje, pa AccountPlan
    assert not {"rate_limit_per_min", "email_rate_limit_per_min"} & (attrs | names)
    assert "enforce" not in names | attrs
    assert "uuid.uuid4" not in src  # asnjë UUID i shpikur (as për Product/Enterprise)


# --- PostgreSQL: dy DB reale + Central → outbox → feed → Enterprise M7-e ---------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_source_connection_is_read_only(seed, cen):
    keys = full_world(seed)
    central_setup(cen, seed, keys)
    from sqlalchemy.exc import InternalError, OperationalError, ProgrammingError

    probe = create_engine(src_url())
    with probe.connect() as c:  # i njëjti mekanizëm që përdor tool-i
        c.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises((InternalError, OperationalError, ProgrammingError)):
            c.execute(text("update sms_account_plans set enabled = false"))
    probe.dispose()
    assert run(cen).ok  # dhe tool-i vetë lexon pa dështuar


def test_end_to_end_bootstrap_to_feed_to_enterprise_entitlements(seed, cen, db):
    """Two-DB proof: Enterprise (burim) → Central (apply) → outbox → feed cp.v1 → M7-e poller →
    sms_entitlements lokale. UUID të ruajtura; AccountPlan e paprekur."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from fastapi.testclient import TestClient

    from app.services import control_plane_client as cc
    from app.services import control_plane_poller as poller
    from apps.central.main import create_app
    from apps.central.services import service_auth
    from tests.test_central_sync_api import keypair

    keys = full_world(seed)
    central_setup(cen, seed, keys)
    plans_before = sdump(db)["sms_account_plans"]
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 3
    eng = create_engine(cen)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        service_auth.create_client(s, "ent-main", ["sync:read"], [seed.ids[k] for k in keys])
        service_auth.add_key(s, "ent-main", "k1", public)
        s.commit()
    c = TestClient(create_app(eng))
    cfg = cc.ControlPlaneConfig("http://testserver", "ent-main", "k1",
                                load_pem_private_key(private.encode(), password=None), 5.0)  # fmt: skip
    client = cc.ControlPlaneClient(cfg, http=c)
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9)
    assert out.ok and out.snapshots == 1
    with SessionLocal() as s:
        from app.models.control_plane import Entitlement

        got = {
            (e.enterprise_id, e.channel): (e.status, e.revision)
            for e in s.scalars(select(Entitlement))
        }
    assert got == {
        (seed.ids["sms_on"], "sms"): ("active", 1),
        (seed.ids["sms_off"], "sms"): ("suspended", 2),
        (seed.ids["mail_ok"], "email"): ("active", 1),
    }
    assert got[(seed.ids["sms_off"], "sms")] == ("suspended", 2)
    # një mutacion i ri i Central rrjedh nga feed-i (jo vetëm snapshot-i)
    with Session(eng, expire_on_commit=False) as s:
        sms_p = s.scalar(select(Product).where(Product.code == "sms"))
        asg.assign_product(s, seed.ids["mail_ok"], sms_p.id)
        s.commit()
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9)
    assert out.ok and out.applied == 1 and out.snapshots == 0
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Entitlement)) == 4
    db.rollback()
    assert (
        sdump(db)["sms_account_plans"] == plans_before
    )  # AccountPlan e paprekur nga i gjithë procesi
    assert db.get(LocalEnterprise, seed.ids["sms_on"]).owner_ref == seed.owner("sms_on")
    eng.dispose()
