# ruff: noqa: F811
"""M11-E2.1 — snapshot i lëshuesit te faturat Enterprise: i pandryshueshëm, pa varësi nga konfigurimi aktual; fallback eksplicit për legacy."""

import ast
import io
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from sqlalchemy import create_engine, event, inspect, text

from app.core.config import settings
from app.models.billing import Invoice, InvoiceImmutableError, InvoiceLine, Subscription
from app.services import billing, invoice_issuer
from tests.test_billing import _key, fresh_gateway, raw_client  # noqa: F401
from tests.test_central import IS_PG, enterprise_alembic, make_db  # noqa: F401
from tests.test_pipeline import world  # noqa: F401

A = {
    "issuer_name": "Alfa Sh.p.k. – Tiranë",
    "issuer_address": "Rruga e Durrësit 1, Tiranë ç ë",
    "issuer_tax_id": "K11111111A",
}
B = {
    "issuer_name": "Beta GmbH & Söhne <b>",
    "issuer_address": "Straße 5, 10115 Berlin",
    "issuer_tax_id": "DE 123 456 789",
}


def text_of(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def set_issuer(monkeypatch, cfg):
    for k, v in cfg.items():
        monkeypatch.setattr(settings, k, v)


@pytest.fixture
def base(db, world, raw_client, monkeypatch):  # noqa: F811
    c = raw_client
    h1, h2, fin = _key(c, "client", "c1"), _key(c, "client", "c2"), _key(c, "finance")
    plan = c.post("/v1/admin/billing/plans", json={"code": "gold", "name": "Gold", "currency": "EUR", "monthly_fee": "10",
                  "included_emails": 5, "email_overage_price": "0.1"}, headers=fin).json()  # fmt: skip
    prof = {
        "legal_name": "Klienti Sh.p.k.",
        "address": "Rruga 1",
        "country": "AL",
        "email": "a@x.example",
        "tax_id": "K1",
        "vat_rate": "0.2",
    }
    c.put("/v1/admin/billing/c1/profile", json=prof, headers=fin)
    c.put(
        "/v1/admin/billing/c1/subscription",
        json={"plan_id": plan["id"], "auto_pay": False},
        headers=fin,
    )
    sub = db.query(Subscription).one()

    def issue(days):
        inv = billing.generate_invoice(db, sub.id, datetime.now(UTC) + timedelta(days=days))
        db.commit()
        return inv

    return SimpleNamespace(c=c, h1=h1, h2=h2, db=db, issue=issue, mp=monkeypatch)


def views(base, inv):
    """→ (api issuer, html, pdf text) — të tre kanalet e faturës."""
    c, h = base.c, base.h1
    api = c.get(f"/v1/billing/invoices/{inv.id}", headers=h).json()
    html = c.get(f"/v1/billing/invoices/{inv.id}/html", headers=h).text
    pdf = text_of(c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=h).content)
    return api, html, pdf


def shows(cfg, html, pdf, api):
    import html as h

    assert api["issuer"] == {
        "name": cfg["issuer_name"],
        "address": cfg["issuer_address"],
        "tax_id": cfg["issuer_tax_id"],
    }
    for v in cfg.values():
        assert h.escape(v) in html, v
        assert v in pdf, v


# ---------------- snapshot ----------------


def test_new_invoice_stores_the_issuer_snapshot_and_only_legal_fields(base):
    set_issuer(base.mp, A)
    inv = base.issue(40)
    assert json.loads(inv.issuer) == {
        "address": A["issuer_address"],
        "name": A["issuer_name"],
        "tax_id": A["issuer_tax_id"],
    }
    assert set(json.loads(inv.issuer)) == {
        "name",
        "address",
        "tax_id",
    }  # asnjë sekret/ID e brendshme
    assert "ç" in inv.issuer  # Unicode ruhet si tekst (ensure_ascii=False)


def test_config_change_never_alters_a_snapshot_backed_invoice_main_acceptance(base):
    """A → faturë → B: detaji i API, HTML dhe PDF vazhdojnë të tregojnë A."""
    set_issuer(base.mp, A)
    inv = base.issue(40)
    api0, html0, pdf0 = views(base, inv)
    shows(A, html0, pdf0, api0)
    set_issuer(base.mp, B)
    api1, html1, pdf1 = views(base, inv)
    shows(A, html1, pdf1, api1)
    for v in B.values():
        assert v not in pdf1 and v not in api1["issuer"].values()
    assert (
        base.c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=base.h1).content
        == base.c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=base.h1).content
    )


def test_second_invoice_after_the_change_shows_b_and_the_first_still_shows_a(base):
    set_issuer(base.mp, A)
    first = base.issue(40)
    set_issuer(base.mp, B)
    second = base.issue(75)
    assert second is not None and second.id != first.id
    shows(A, *views(base, first)[1:], views(base, first)[0])
    shows(B, *views(base, second)[1:], views(base, second)[0])


def test_no_current_config_is_read_when_a_snapshot_exists(base):
    set_issuer(base.mp, A)
    inv = base.issue(40)

    def boom():
        raise AssertionError("current issuer config must not be read for a snapshot-backed invoice")

    base.mp.setattr(invoice_issuer, "current_issuer", boom)
    api, html, pdf = views(base, inv)
    shows(A, html, pdf, api)


def test_unusual_issuer_values_are_preserved_exactly(base):
    odd = {
        "issuer_name": '  Spaced & <Odd> "Co" ß  ',
        "issuer_address": "Line 1\nLine 2 — çë",
        "issuer_tax_id": "€ 0000-X/9",
    }
    set_issuer(base.mp, odd)
    inv = base.issue(40)
    assert json.loads(inv.issuer)["name"] == odd["issuer_name"]
    api = base.c.get(f"/v1/billing/invoices/{inv.id}", headers=base.h1).json()
    assert (
        api["issuer"]["name"] == odd["issuer_name"]
        and api["issuer"]["address"] == odd["issuer_address"]
        and api["issuer"]["tax_id"] == odd["issuer_tax_id"]
    )
    pdf = text_of(base.c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=base.h1).content)
    assert (
        "Spaced & <Odd>" in pdf and "€ 0000-X/9" in pdf and "Line 2 — çë" in pdf.replace("\n", " ")
    )


def test_issuer_snapshot_is_immutable_in_the_orm(base):
    set_issuer(base.mp, A)
    inv = base.issue(40)
    inv.issuer = json.dumps({"name": "Hacked", "address": "x", "tax_id": "y"})
    with pytest.raises(InvoiceImmutableError):
        base.db.flush()
    base.db.rollback()


def test_financial_values_are_unchanged_by_the_snapshot(base):
    set_issuer(base.mp, A)
    inv = base.issue(40)
    assert (D(inv.subtotal), D(inv.tax), D(inv.total)) == (D("10"), D("2"), D("12"))
    api = base.c.get(f"/v1/billing/invoices/{inv.id}", headers=base.h1).json()
    assert (api["subtotal"], api["tax"], api["total"]) == ("10.000000", "2.000000", "12.000000")
    assert set(api) == {"id", "number", "status", "currency", "subtotal", "vat_rate", "tax", "total", "issued_at", "due_at", "paid_at", "paid_via",
                        "period_start", "period_end", "issuer", "lines"}  # fmt: skip


def test_list_endpoint_is_unchanged(base):
    set_issuer(base.mp, A)
    base.issue(40)
    item = base.c.get("/v1/billing/invoices", headers=base.h1).json()[0]
    assert "issuer" not in item and "lines" not in item


def test_tenant_scoping_is_unchanged(base):
    set_issuer(base.mp, A)
    inv = base.issue(40)
    for suffix in ("", "/pdf", "/html"):
        assert (
            base.c.get(f"/v1/billing/invoices/{inv.id}{suffix}", headers=base.h2).status_code == 404
        )


def test_every_invoice_creation_path_is_covered():
    """Statik: `Invoice(...)` instancohet vetëm te `generate_invoice` (faturimi periodik), dhe aty merr issuer-in."""
    hits = []
    for f in Path("app").rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Invoice":
                hits.append((str(f), {k.arg for k in n.keywords}))
    assert [h[0] for h in hits] == ["app/services/billing.py"]
    assert "issuer" in hits[0][1] and "bill_to" in hits[0][1]


def test_recurring_billing_goes_through_the_snapshot(base, db):
    set_issuer(base.mp, A)
    base.issue(40)
    n = db.query(Invoice).filter(Invoice.issuer.is_(None)).count()
    assert n == 0  # asnjë faturë e re pa snapshot


# ---------------- legacy ----------------


def legacy_invoice(db, base_inv):
    """Faturë legacy: i njëjti rresht pa issuer (siç ekzistonin para migrimit)."""
    inv = Invoice(number="INV-LEGACY-000001", owner_ref=base_inv.owner_ref, subscription_id=None, period_start=base_inv.period_start - timedelta(days=90),
                  period_end=base_inv.period_start - timedelta(days=60), currency="EUR", subtotal=D("5"), vat_rate=D("0.2"), tax=D("1"), total=D("6"),
                  bill_to=base_inv.bill_to, issuer=None, issued_at=base_inv.issued_at - timedelta(days=60), due_at=base_inv.due_at - timedelta(days=60))  # fmt: skip
    db.add(inv)
    db.flush()
    db.add(
        InvoiceLine(
            invoice_id=inv.id,
            description="Legacy fee",
            quantity=D("1"),
            unit_price=D("5"),
            amount=D("5"),
            pricing_source="legacy_plan",
        )
    )
    db.commit()
    return inv


def test_legacy_invoice_renders_with_an_explicit_deterministic_fallback(base, db):
    set_issuer(base.mp, A)
    modern = base.issue(40)
    legacy = legacy_invoice(db, modern)
    assert invoice_issuer.issuer_of(legacy)[1] == invoice_issuer.LEGACY_FALLBACK
    assert invoice_issuer.issuer_of(modern)[1] == invoice_issuer.SNAPSHOT
    api, html, pdf = views(base, legacy)
    shows(A, html, pdf, api)  # HTML, PDF dhe API pajtohen (fallback = konfigurimi aktual)
    assert (
        "snapshot" not in json.dumps(api).lower() and "fallback" not in json.dumps(api).lower()
    )  # provenienca s'ekspozohet
    assert D(api["total"]) == D("6") and api["lines"][0]["description"] == "Legacy fee"
    set_issuer(
        base.mp, B
    )  # fallback është EKSPLICIT: ndjek konfigurimin aktual, s'shpik histori; modern mbetet A
    api2, html2, pdf2 = views(base, legacy)
    shows(B, html2, pdf2, api2)
    shows(A, *views(base, modern)[1:], views(base, modern)[0])


def test_legacy_rows_are_never_backfilled_by_the_application(base, db):
    set_issuer(base.mp, A)
    legacy = legacy_invoice(db, base.issue(40))
    views(base, legacy)
    db.expire_all()
    assert db.get(Invoice, legacy.id).issuer is None


def test_no_secrets_in_the_snapshot_source():
    cur = invoice_issuer.current_issuer()
    assert set(cur) == {"name", "address", "tax_id"}
    assert not re.search(r"key|secret|password|token|smtp", json.dumps(cur), re.I)


# ---------------- migrimi ----------------


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if "sms_invoices" in repr(i)
    ]


def test_enterprise_0033_is_additive_reversible_matches_metadata_and_keeps_legacy_null(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0032")
    eng = create_engine(url)
    cols = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert "issuer" not in cols["sms_invoices"]
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with eng.begin() as c:  # faturë legacy e vërtetë: ekziston para migrimit
        c.execute(text(
            "INSERT INTO sms_invoices (number, owner_ref, period_start, period_end, currency, subtotal, vat_rate, tax, total, status, bill_to, issued_at, due_at) "
            "VALUES ('INV-OLD-1','c1', :a, :b, 'EUR', 5, 0.2, 1, 6, 'OPEN', '{\"legal_name\":\"Old\",\"address\":\"A\",\"country\":\"AL\"}', :a, :b)"), {"a": now, "b": now + timedelta(days=30)})  # fmt: skip
    enterprise_alembic(url, "upgrade", "0033")
    after = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert after["sms_invoices"] == cols["sms_invoices"] | {"issuer"}
    assert {t: v for t, v in after.items() if t != "sms_invoices"} == {
        t: v for t, v in cols.items() if t != "sms_invoices"
    }
    col = next(c for c in inspect(eng).get_columns("sms_invoices") if c["name"] == "issuer")
    assert col["nullable"] is True
    assert "issuer" not in {
        c for i in inspect(eng).get_indexes("sms_invoices") for c in i["column_names"]
    }  # pa indeks
    with eng.connect() as c:
        assert (
            c.execute(text("SELECT issuer FROM sms_invoices WHERE number='INV-OLD-1'")).scalar()
            is None
        )  # asnjë backfill
        assert _drift(c) == []
    enterprise_alembic(url, "downgrade", "0032")
    assert "issuer" not in {c["name"] for c in inspect(eng).get_columns("sms_invoices")}
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _drift(c) == []
        assert (
            c.execute(text("SELECT count(*) FROM sms_invoices WHERE number='INV-OLD-1'")).scalar()
            == 1
        )
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_guard_blocks_any_issuer_change_even_null_to_value(make_db):
    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO sms_invoices (number, owner_ref, period_start, period_end, currency, subtotal, vat_rate, tax, total, status, bill_to, issuer, issued_at, due_at) "
            "VALUES ('INV-N-1','c1', :a, :b, 'EUR', 5, 0.2, 1, 6, 'OPEN', '{}', '{\"name\":\"A\"}', :a, :b), ('INV-N-2','c1', :a, :b, 'EUR', 5, 0.2, 1, 6, 'OPEN', '{}', NULL, :a, :b)"), {"a": now, "b": now + timedelta(days=30)})  # fmt: skip
    from sqlalchemy.exc import DBAPIError

    for number, new in (
        ("INV-N-1", '{"name":"B"}'),
        ("INV-N-1", None),
        ("INV-N-2", '{"name":"X"}'),
    ):
        with pytest.raises(DBAPIError):
            with eng.begin() as c:
                c.execute(
                    text("UPDATE sms_invoices SET issuer = :v WHERE number = :n"),
                    {"v": new, "n": number},
                )
    with eng.connect() as c:
        assert (
            c.execute(text("SELECT issuer FROM sms_invoices WHERE number='INV-N-1'")).scalar()
            == '{"name":"A"}'
        )
    eng.dispose()


# ---------------- performanca ----------------


def test_no_extra_queries_for_html_and_pdf(base, db):
    set_issuer(base.mp, A)
    inv = base.issue(40)
    out = {}
    for name, path in (("html", "/html"), ("pdf", "/pdf"), ("detail", "")):
        n = []
        listener = lambda conn, cur, stmt, *a, n=n: n.append(stmt)  # noqa: E731
        bind = db.get_bind()
        event.listen(bind, "before_cursor_execute", listener)
        try:
            assert (
                base.c.get(f"/v1/billing/invoices/{inv.id}{path}", headers=base.h1).status_code
                == 200
            )
        finally:
            event.remove(bind, "before_cursor_execute", listener)
        out[name] = len(n)
    print("QUERIES", out)
    assert (
        out["pdf"] <= 5 and out["html"] <= 5 and out["detail"] <= 5
    )  # i njëjti numër si para snapshot-it (PDF: 5)
