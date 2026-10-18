# ruff: noqa: F811
"""M11-E2 — Central: filtra faturash (numër, data), filtri enterprise te pagesat e kreditit, treguesi credit-note, PDF zyrtar."""

import io
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from pypdf import PdfReader
from sqlalchemy import event, select

from apps.central.models.billing import Invoice
from apps.central.services import (
    billing,
    credit_accounts,
    credit_notes,
    enterprises,
    invoice_document,
    payments,
)
from apps.central.services import products as prod
from packages.invoice_pdf import InvoiceDoc, LineDoc, render_invoice_pdf, safe_filename
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import (  # noqa: F401
    T0,
    U,
    api,
    b,
    mk_plan,
    process,
    profile,
    subscribe,
)

INV = "/admin/billing/invoices"


def text_of(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def invoice_at(b, now, *, eid=None, name="Acme Sh.p.k.", fee="10", vat="0.2"):
    """Faturë reale OPEN lëshuar në `now` (UTC) me enterprise të ri; → (invoice_id, enterprise_id)."""
    with b.F() as s:
        eid = eid or enterprises.create(s, f"Co-{uuid.uuid4().hex[:6]}").id
        s.commit()
        billing.set_profile(
            s,
            U(s, b.a1),
            eid,
            name,
            "Rr. Kryesore 1, Tiranë",
            "al",
            "b@example.test",
            "K12345678A",
            vat,
        )
        s.commit()
    vid = mk_plan(b, fee=fee, code=f"p{uuid.uuid4().hex[:6]}")
    sid = subscribe(b, vid, eid=eid)
    r = process(b, sid, now=now)
    assert r.invoice is not None
    return r.invoice.id, eid


def ids(resp):
    assert resp.status_code == 200, resp.text
    return [i["id"] for i in resp.json()["items"]]


D1 = datetime(2030, 2, 16, 12, tzinfo=UTC)
D2 = datetime(2030, 2, 17, 23, 30, tzinfo=UTC)
D3 = datetime(2030, 2, 18, 0, 0, tzinfo=UTC)
D4 = datetime(2030, 3, 1, 8, tzinfo=UTC)


@pytest.fixture
def four(b, api):
    out = [invoice_at(b, d)[0] for d in (D1, D2, D3, D4)]
    return [str(i) for i in out]


# ---------------- filtra ----------------


def test_unfiltered_list_is_unchanged_plus_additive_fields(api, four):
    r = api.get(INV, headers=api.h["ro"])
    assert ids(r) == [four[3], four[2], four[1], four[0]]  # issued_at DESC
    item = r.json()["items"][0]
    base = {"id", "number", "enterprise_id", "subscription_id", "period_index", "period_start", "period_end", "plan_version_id",
            "currency", "subtotal", "vat_rate", "tax", "total", "status", "bill_to", "issuer", "issued_at", "due_at", "paid_at",
            "voided_at", "voided_reason"}  # fmt: skip
    assert base <= set(item) and set(item) - base == {"credit_note_count", "credited_total"}
    assert (item["credit_note_count"], item["credited_total"]) == (0, "0.000000")


def test_number_filter_is_a_safe_prefix(api, four):
    h = api.h["ro"]
    nums = [i["number"] for i in api.get(INV, headers=h).json()["items"]]
    assert len(nums) == 4
    assert ids(api.get(INV, params={"number": nums[0]}, headers=h)) == [
        four[3]
    ]  # numri i plotë = përputhje e saktë
    assert len(ids(api.get(INV, params={"number": "INV-2030"}, headers=h))) == 4  # parafytyrë
    assert (
        len(ids(api.get(INV, params={"number": "inv-2030"}, headers=h))) == 4
    )  # hyrja bëhet e madhe
    assert ids(api.get(INV, params={"number": "ZZZ"}, headers=h)) == []
    assert (
        ids(api.get(INV, params={"number": "INV_2030"}, headers=h)) == []
    )  # "_" nuk është wildcard
    for bad in ("%", "a b", "x" * 25, "INV-2030%"):
        assert api.get(INV, params={"number": bad}, headers=h).status_code == 422


def test_date_range_is_inclusive_utc_calendar_days(api, four):
    h = api.h["ro"]
    g = lambda **p: ids(api.get(INV, params=p, headers=h))  # noqa: E731
    assert g(issued_from="2030-02-18") == [four[3], four[2]]  # 00:00:00Z përfshihet
    assert g(issued_to="2030-02-17") == [
        four[1],
        four[0],
    ]  # 23:30Z e ditës 17 përfshihet, 00:00Z e 18 jo
    assert g(issued_from="2030-02-17", issued_to="2030-02-17") == [four[1]]
    assert g(issued_from="2030-02-17", issued_to="2030-02-18") == [four[2], four[1]]
    assert g(issued_from="2030-04-01") == [] and g(issued_to="2030-01-01") == []


def test_date_validation_is_structured(api, four):
    h = api.h["ro"]
    for bad in ("2030-13-01", "garbage", "2030-02-30", ""):
        r = api.get(INV, params={"issued_from": bad}, headers=h)
        assert r.status_code == 422, bad
    r = api.get(INV, params={"issued_to": "nope"}, headers=h)
    assert r.status_code == 422 and any(e["loc"][-1] == "issued_to" for e in r.json()["detail"])
    r = api.get(INV, params={"issued_from": "2030-03-02", "issued_to": "2030-03-01"}, headers=h)
    assert r.status_code == 422
    d = r.json()["detail"]
    assert d["code"] == "invalid" and "issued_from" in d["message"] and "issued_to" in d["message"]


def test_filters_compose_and_paginate_deterministically(b, api, four):
    h = api.h["ad"]
    with b.F() as s:
        e = s.scalar(select(Invoice.enterprise_id).where(Invoice.id == uuid.UUID(four[1])))
    assert ids(
        api.get(
            INV,
            params={"enterprise_id": str(e), "issued_from": "2030-02-17", "status": "open"},
            headers=h,
        )
    ) == [four[1]]
    assert ids(api.get(INV, params={"enterprise_id": str(e), "status": "paid"}, headers=h)) == []
    assert ids(
        api.get(
            INV,
            params={
                "number": "INV-2030",
                "issued_from": "2030-02-17",
                "issued_to": "2030-02-18",
                "status": "open",
            },
            headers=h,
        )
    ) == [four[2], four[1]]
    seen, off = [], 0
    while True:
        r = api.get(INV, params={"number": "INV-", "limit": 1, "offset": off}, headers=h).json()
        seen += [i["id"] for i in r["items"]]
        if not r["has_more"]:
            break
        off += 1
    assert seen == [four[3], four[2], four[1], four[0]]
    assert ids(api.get(INV, params={"number": "INV-", "limit": 2, "offset": 2}, headers=h)) == [
        four[1],
        four[0],
    ]


def test_credit_note_indicator_has_no_n_plus_one(b, api, four):
    """Pas credit note: count/total nga NJË pyetje të grupuar; numri i SELECT-eve s'rritet me numrin e faturave."""
    with b.F() as s:
        a = U(s, b.a1)
        for n, iid in enumerate(four[:3]):
            amt = str(billing.get_invoice(s, iid).total)
            ip = __import__("apps.central.services.invoice_payments", fromlist=["x"])
            p = ip.create(s, iid, amt, actor=a, external_reference=f"r{n}")
            ip.approve(s, p.id, U(s, b.a2))
        s.commit()
        credit_notes.issue(s, a, four[0], "1", "first", "key-note-a001")
        credit_notes.issue(s, a, four[0], "2.5", "second", "key-note-a002")
        credit_notes.issue(s, a, four[1], "3", "other", "key-note-a003")
        s.commit()
    r = api.get(INV, headers=api.h["ro"]).json()["items"]
    by = {i["id"]: (i["credit_note_count"], i["credited_total"]) for i in r}
    assert (
        by[four[0]] == (2, "3.500000")
        and by[four[1]] == (1, "3.000000")
        and by[four[2]] == (0, "0.000000")
        and by[four[3]] == (0, "0.000000")
    )

    def selects(**params):
        n = []
        listener = lambda conn, cur, stmt, *a: (
            n.append(stmt) if stmt.lstrip().upper().startswith("SELECT") else None
        )  # noqa: E731
        event.listen(b.eng, "before_cursor_execute", listener)
        try:
            api.get(INV, params=params, headers=api.h["ro"])
        finally:
            event.remove(b.eng, "before_cursor_execute", listener)
        return len(n)

    assert selects(limit=1) == selects(limit=4)  # i njëjti numër pyetjesh për 1 apo 4 fatura


# ---------------- pagesat e kreditit ----------------


def test_credit_payments_enterprise_filter(b, api):
    with b.F() as s:
        a = U(s, b.a1)
        product = prod.create(s, "sms", "SMS", "sms")
        acc1 = credit_accounts.create(s, b.e1, product.id, "EUR", a)
        acc2 = credit_accounts.create(s, b.e2, product.id, "EUR", a)
        p1 = payments.create(s, acc1.id, "10", actor=a, external_reference="c1")
        p2 = payments.create(s, acc1.id, "11", actor=a, external_reference="c2")
        p3 = payments.create(s, acc2.id, "12", actor=a, external_reference="c3")
        payments.approve(s, p2.id, U(s, b.a2))
        s.commit()
        p1, p2, p3 = str(p1.id), str(p2.id), str(p3.id)
    P = "/admin/money/payments"
    h = api.h["ro"]
    g = lambda **q: ids(api.get(P, params=q, headers=h))  # noqa: E731
    assert set(g()) == {p1, p2, p3}  # pa filtër: i pandryshuar
    assert set(g(enterprise_id=str(b.e1))) == {p1, p2} and g(enterprise_id=str(b.e2)) == [p3]
    assert g(enterprise_id=str(b.e1), status="approved") == [p2]
    assert g(enterprise_id=str(uuid.uuid4())) == []  # enterprise i panjohur ⇒ listë bosh
    assert api.get(P, params={"enterprise_id": "not-a-uuid"}, headers=h).status_code == 422
    page1 = api.get(P, params={"enterprise_id": str(b.e1), "limit": 1}, headers=h).json()
    assert page1["has_more"] and len(page1["items"]) == 1
    assert ids(
        api.get(P, params={"enterprise_id": str(b.e1), "limit": 1, "offset": 1}, headers=h)
    ) != [page1["items"][0]["id"]]


# ---------------- PDF ----------------


def test_pdf_central_admin_and_operator_read(b, api):
    iid, eid = invoice_at(b, D1, name="Müller & Söhne GmbH – Zürich")
    for who in ("ad", "ro"):
        r = api.get(f"{INV}/{iid}/pdf", headers=api.h[who])
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        cd = r.headers["content-disposition"]
        assert (
            cd.startswith('attachment; filename="invoice-INV-2030-')
            and cd.endswith('.pdf"')
            and "\n" not in cd
        )
        assert (
            r.headers["cache-control"] == "private, no-store"
            and r.headers["x-content-type-options"] == "nosniff"
        )
        assert r.content.startswith(b"%PDF-") and int(r.headers["content-length"]) == len(r.content)
    assert (
        api.get(f"{INV}/{iid}/pdf", params={"inline": "true"}, headers=api.h["ro"])
        .headers["content-disposition"]
        .startswith("inline;")
    )
    with b.F() as s:
        inv = s.get(Invoice, iid)
        t = text_of(r.content)
        assert inv.number in t
        assert (
            "Müller & Söhne GmbH – Zürich" in t and "Rr. Kryesore 1, Tiranë" in t
        )  # bill-to snapshot, Unicode
        assert inv.issuer["name"] in t and "From" in t  # issuer snapshot
        assert "monthly fee" in t and "EUR" in t
        assert (
            f"{D(inv.total):,.2f}" in t
            and f"{D(inv.tax):,.2f}" in t
            and f"{D(inv.subtotal):,.2f}" in t
        )


def test_pdf_uses_historical_snapshot_not_current_profile(b, api):
    iid, eid = invoice_at(b, D1, name="Old Name Sh.p.k.")
    before = api.get(f"{INV}/{iid}/pdf", headers=api.h["ad"]).content
    with b.F() as s:
        billing.set_profile(
            s,
            U(s, b.a1),
            eid,
            "Brand New Name",
            "Elsewhere 9",
            "al",
            "n@example.test",
            "K999",
            "0.2",
        )
        s.commit()
    after = api.get(f"{INV}/{iid}/pdf", headers=api.h["ad"]).content
    assert "Old Name Sh.p.k." in text_of(after) and "Brand New Name" not in text_of(after)
    assert before == after  # reprodhueshmëri: të njëjtat bajte


def test_pdf_credit_notes_are_listed_and_totals_come_from_storage(b, api):
    iid, _ = invoice_at(b, D1)
    with b.F() as s:
        total = str(billing.get_invoice(s, iid).total)
        a = U(s, b.a1)
        from apps.central.services import invoice_payments as ip

        p = ip.create(s, iid, total, actor=a, external_reference="x1")
        ip.approve(s, p.id, U(s, b.a2))
        s.commit()
        n = credit_notes.issue(s, a, iid, "1.25", "goodwill", "key-note-pdf1")
        s.commit()
        number = n.number
    t = text_of(api.get(f"{INV}/{iid}/pdf", headers=api.h["ro"]).content)
    assert number in t and "goodwill" in t and "1.25" in t


def test_renderer_prints_persisted_totals_as_stored_never_recomputes():
    """Totali i ruajtur është qëllimisht i pazakontë (≠ nëntotal + tatim, ≠ shuma e rreshtave): PDF e shfaq siç është."""
    doc = InvoiceDoc("INV-9", "open", "EUR", "2030-01-01", "2030-01-15", "2029-12-01", "2030-01-01", {"legal_name": "I"}, {"legal_name": "B"},
                     [LineDoc("one", "1", "1.000000", "1.000000"), LineDoc("two", "2", "2.000000", "4.000000")],
                     "5.000000", "0.2000", "1.000000", "1234567.890000")  # fmt: skip
    t = text_of(render_invoice_pdf(doc))
    assert "1,234,567.89 EUR" in t and "6.00" not in t and "5.00 EUR" in t
    assert render_invoice_pdf(doc) == render_invoice_pdf(doc)


def test_renderer_unicode_and_markup_are_safe():
    names = [
        "Shqipëria Çka Ëndrra",
        "Müller & Söhne GmbH – Zürich",
        "Straße Ünïcödé",
        "<b>x</b> & <script>",
    ]
    doc = InvoiceDoc("INV-1", "open", "EUR", "2030-01-01", "2030-01-02", "a", "b", {"legal_name": names[0]}, {"legal_name": names[1], "address": names[2]},
                     [LineDoc(names[3], "1", "1", "1")], "1", "0", "0", "1")  # fmt: skip
    t = text_of(render_invoice_pdf(doc))
    for n in names:
        assert n in t


@pytest.mark.parametrize(
    "raw,expect",
    [
        ("INV-2030-000001", "invoice-INV-2030-000001.pdf"),
        ("a/../b", "invoice-a_._b.pdf"),
        ("x\r\ny", "invoice-x_y.pdf"),
        ("ü€", "invoice-u.pdf"),
        ("", "invoice-invoice.pdf"),
        ("..", "invoice-invoice.pdf"),
        ("a" * 100, "invoice-" + "a" * 48 + ".pdf"),
    ],
)
def test_filename_is_sanitized(raw, expect):
    assert safe_filename(raw) == expect


def test_pdf_errors_are_clean(b, api, monkeypatch, caplog):
    iid, _ = invoice_at(b, D1)
    assert api.get(f"{INV}/{uuid.uuid4()}/pdf", headers=api.h["ro"]).status_code == 404
    assert api.get(f"{INV}/not-a-uuid/pdf", headers=api.h["ro"]).status_code == 422
    assert api.get(f"{INV}/{iid}/pdf").status_code in (401, 403)

    def boom(doc):
        raise RuntimeError("secret internal detail /srv/path")

    monkeypatch.setattr(invoice_document, "render_invoice_pdf", boom)
    with caplog.at_level(logging.ERROR):
        r = api.get(f"{INV}/{iid}/pdf", headers=api.h["ro"])
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"] == {
        "code": "pdf_render_failed",
        "message": "the invoice PDF could not be generated",
    }
    assert "secret" not in r.text and "Traceback" not in r.text
    assert any(
        "invoice pdf render failed" in rec.message for rec in caplog.records
    )  # log operacional
    with b.F() as s:  # faturat s'preken
        assert s.get(Invoice, iid).status == "open"


def test_pdf_is_bounded_and_read_only(b, api):
    iid, _ = invoice_at(b, D1)
    n = []
    listener = lambda conn, cur, stmt, *a: n.append(stmt)  # noqa: E731
    event.listen(b.eng, "before_cursor_execute", listener)
    try:
        assert api.get(f"{INV}/{iid}/pdf", headers=api.h["ro"]).status_code == 200
    finally:
        event.remove(b.eng, "before_cursor_execute", listener)
    writes = [s for s in n if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(n) <= 14 and writes == []


def test_no_new_mutation_routes(api):
    ops = api.app.openapi()["paths"]
    for path, methods in ops.items():
        if path.endswith("/pdf"):
            assert set(methods) == {"get"}
    assert set(ops["/admin/billing/invoices"]) == {"get"}


def test_central_0031_adds_only_two_indexes_reversibly_and_matches_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, inspect

    import apps.central.models  # noqa: F401
    from apps.central.core.db import Base

    url = make_db()
    central_alembic(url, "upgrade", "0030")
    eng = create_engine(url)
    cols = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    idx = lambda: {i["name"] for i in inspect(eng).get_indexes("invoices")}  # noqa: E731
    assert not {"ix_invoices_issued_at", "ix_invoices_number_prefix"} & idx()

    def drift():
        with eng.connect() as c:
            ctx = MigrationContext.configure(
                c, opts={"compare_type": True, "version_table": "central_alembic_version"}
            )
            return compare_metadata(ctx, Base.metadata)

    central_alembic(url, "upgrade", "0031")
    assert {"ix_invoices_issued_at", "ix_invoices_number_prefix"} <= idx()
    assert {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in cols
    } == cols  # asnjë kolonë e ndryshuar
    assert drift() == []
    central_alembic(url, "downgrade", "0030")
    assert not {"ix_invoices_issued_at", "ix_invoices_number_prefix"} & idx()
    central_alembic(url, "upgrade", "head")
    assert drift() == []
    eng.dispose()
