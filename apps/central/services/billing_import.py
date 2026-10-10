"""M9-g4: importi i faturimit legacy të Enterprise në Central (artifact offline `cp.billing.legacy_export.v1`). Pa HTTP, pa commit (transaksioni i thirrësit).

Parime: dry-run i pastër (zero shkrime) · idempotent (UNIQUE `export_id`, UNIQUE (source_system, source_table, source_id)) · hash-i i artifact-it kontrollohet (`evidence_hash`) ·
asnjë mutacion i rreshtave legacy · asnjë coercion e heshtur: çdo rresht klasifikohet `exact | importable | already_imported | conflict | invalid | unsupported |
requires_manual_review`; rreshtat bllokues regjistrohen si `BillingImportIssue` (kurrë të humbur) dhe blloku autoritetin `central` derisa të zgjidhen.

Historia e panjohur mbetet e shprehur: periudha legacy pa faturë NUK bëhen `no_charge` (s'ka prova); fatura legacy marrin `provenance=legacy_import`, `plan_version_id` NULL dhe issuer
`{"provenance": "unknown"}` (s'ishte ngrirë historikisht); fatura nga segmenti i një ankore të mëparshme (rifillim) marrin `period_index = -source_id`.
Fatura `paid` importohen si gjendje historike terminale me pagesë+alokim eksplicit (burimi `legacy_import`; wallet-i NUK rilozet, as nuk krijohet kredi tregtare)."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    INV_OPEN,
    INV_PAID,
    INV_VOID,
    L_EMAIL_OVERAGE,
    L_LEGACY,
    L_MONTHLY_FEE,
    P_INVOICED,
    PROV_LEGACY,
    SUB_ACTIVE,
    SUB_CANCELLED,
    V_RETIRED,
    BillingPeriod,
    BillingProfile,
    BillingSubscription,
    CommercialPlan,
    Invoice,
    InvoiceLine,
    InvoiceNumberSequence,
    PlanVersion,
)
from apps.central.models.billing_import import (
    BLOCKING,
    BillingImportBatch,
    BillingImportIssue,
    BillingImportItem,
    BillingUsageBaseline,
)
from apps.central.models.enterprise import Enterprise
from apps.central.models.money import APPROVED, PURPOSE_INVOICE, Payment
from apps.central.models.pricing import PriceVersion
from apps.central.models.product import Product
from apps.central.models.settlement import InvoicePaymentAllocation
from apps.central.services import audit, billing, billing_plans, money_common
from packages.contracts.control_plane.billing import legacy_export_v1 as lx

SOURCE = "enterprise"
SYSTEM = "system:billing_import"
_INV_NUM = re.compile(r"^INV-(\d{4})-(\d{6,})$")
_OVERAGE = re.compile(r"^Email overage \((\d+) above (\d+) included\)$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_EMAIL = billing._EMAIL
Q6 = Decimal("0.000001")
Q4 = Decimal("0.0001")
VOID_DEFAULT_REASON = "legacy void (no reason recorded at the source)"


def h(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str).encode()
    ).hexdigest()


def D(x) -> Decimal:
    return Decimal(x)


def T(x: str) -> datetime:
    return datetime.fromisoformat(x)


@dataclass(slots=True)
class Decision:
    table: str
    source_id: str
    classification: str
    reason: str = ""
    action: str = "none"  # create | link | advance | none
    core_hash: str = ""
    state_hash: str = ""
    data: dict = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.classification in BLOCKING


@dataclass(slots=True)
class ImportPlan:
    doc: dict
    decisions: list[Decision] = field(default_factory=list)
    seeds: dict[int, int] = field(default_factory=dict)
    baselines: list[Decision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by(self, table: str) -> list[Decision]:
        return [d for d in self.decisions if d.table == table]

    def summary(self) -> dict:
        out: dict = {}
        for d in [*self.decisions, *self.baselines]:
            out.setdefault(d.table, {}).setdefault(d.classification, 0)
            out[d.table][d.classification] += 1
        return out

    def blocking(self) -> list[Decision]:
        return [d for d in [*self.decisions, *self.baselines] if d.blocked]

    def report(self) -> dict:
        return {
            "export_id": self.doc["export_id"], "content_hash": self.doc["content_hash"], "summary": self.summary(),
            "blocking": [{"table": d.table, "source_id": d.source_id, "classification": d.classification, "reason": d.reason} for d in self.blocking()][:200],
            "blocking_total": len(self.blocking()), "sequence_seeds": {str(y): n for y, n in sorted(self.seeds.items())},
            "authority": self.doc["authority"], "notes": self.notes,
            "blocking_by_category": self.blocking_by_category(),
            "proposed_baselines": [{"enterprise_id": d.data["enterprise_id"], "boundary": d.data["boundary"], "cumulative": d.data["cumulative_before_boundary"],
                                    "watermark": d.data["watermark_before_boundary"], "classification": d.classification} for d in self.baselines],
        }  # fmt: skip

    def blocking_by_category(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.blocking():
            c = categorize(d.reason)
            out[c] = out.get(c, 0) + 1
        return dict(sorted(out.items()))


# --- klasifikimi (vetëm lexim) ------------------------------------------------------------------------------------------------------------------


def _items(db: Session) -> dict[tuple[str, str], BillingImportItem]:
    return {(i.source_table, i.source_id): i for i in db.scalars(select(BillingImportItem))}


def line_type(desc: str, qty: Decimal) -> str:
    """Rregull eksplicit (dokumentuar): tekstet që gjeneron `app.services.billing._invoice_lines`; çdo gjë tjetër mbetet `legacy`."""
    m = _OVERAGE.match(desc)
    if m and qty == Decimal(int(m.group(1))):
        return L_EMAIL_OVERAGE
    if desc.endswith(" - monthly fee") and qty == 1:
        return L_MONTHLY_FEE
    return L_LEGACY


def plan_import(db: Session, doc: dict, *, authority: str = "local") -> ImportPlan:
    lx.parse(doc)
    plan = ImportPlan(doc=doc)
    items = _items(db)
    enterprises = {e for e in db.scalars(select(Enterprise.id))}
    d_plans = _plans(db, doc, items, plan)
    d_profiles = _profiles(db, doc, items, enterprises, authority, plan)
    d_subs = _subscriptions(db, doc, items, enterprises, d_plans, d_profiles, authority, plan)
    _invoices(db, doc, items, d_subs, d_plans, authority, plan)
    _sequences(db, doc, plan)
    _baselines(db, doc, items, enterprises, d_subs, plan)
    return plan


def _plans(db, doc, items, plan) -> dict[int, Decision]:
    out: dict[int, Decision] = {}
    for p in doc["plans"]:
        sid = str(p["source_id"])
        core = {
            "code": p["code"],
            "name": p["name"],
            "currency": p["currency"],
            "monthly_fee": p["monthly_fee"],
            "included_emails": p["included_emails"],
        }
        d = Decision(
            "plans",
            sid,
            "importable",
            core_hash=h(core),
            state_hash=h({"status": p["status"]}),
            data={**p, "core": core},
        )
        try:
            if not billing_plans.CODE.match(p["code"]) or not _CURRENCY.match(p["currency"]):
                raise Invalid("code/currency do not satisfy the Central plan rules")
            billing_plans.fee(p["monthly_fee"])
            billing_plans.included(p["included_emails"])
            if not p["name"].strip():
                raise Invalid("plan name is empty")
        except Invalid as e:
            d.classification, d.reason = "invalid", str(e)
        else:
            item = items.get(("plans", sid))
            if item is not None:
                if item.source_hash != d.core_hash:
                    d.classification, d.reason = (
                        "conflict",
                        "plan content changed at the source after import (plans are immutable)",
                    )
                elif item.state_hash == d.state_hash:
                    d.classification = "already_imported"
                elif p["status"] == "retired":
                    d.classification, d.action = "importable", "advance"
                else:
                    d.classification, d.reason = (
                        "conflict",
                        "retired plan cannot become active again",
                    )
            else:
                cp = db.scalar(select(CommercialPlan).where(CommercialPlan.code == p["code"]))
                if cp is not None:
                    vs = billing_plans.versions_of(db, cp.id)
                    v = vs[0] if len(vs) == 1 else None
                    if v is not None and (v.currency, D(v.monthly_fee), v.included_emails) == (
                        p["currency"],
                        D(p["monthly_fee"]),
                        p["included_emails"],
                    ):
                        d.classification, d.action = "exact", "link"
                        d.data["existing_version_id"] = str(v.id)
                    else:
                        d.classification, d.reason = (
                            "conflict",
                            "a different Central plan with this code already exists",
                        )
                else:
                    d.action = "create"
        out[p["source_id"]] = d
        plan.decisions.append(d)
    return out


def _profiles(db, doc, items, enterprises, authority, plan) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for p in doc["profiles"]:
        sid = str(p["source_id"])
        fields = {
            k: p[k] for k in ("legal_name", "address", "country", "tax_id", "email", "vat_rate")
        }
        d = Decision(
            "profiles",
            sid,
            "importable",
            core_hash=h({"enterprise_id": p["enterprise_id"], "owner_ref": p["owner_ref"]}),
            state_hash=h(fields),
            data=p,
        )
        eid = p["enterprise_id"]
        try:
            if eid is None:
                d.classification, d.reason = (
                    "invalid",
                    "profile has no enterprise_id (cannot be mapped to Central)",
                )
            elif uuid.UUID(eid) not in enterprises:
                d.classification, d.reason = (
                    "requires_manual_review",
                    "enterprise does not exist in Central",
                )
            else:
                if (
                    not p["legal_name"].strip()
                    or len(p["legal_name"]) > 120
                    or not p["address"].strip()
                    or len(p["address"]) > 300
                ):
                    raise Invalid("legal_name/address are required within the Central limits")
                if not re.fullmatch(r"[A-Za-z]{2}", p["country"]) or not _EMAIL.match(
                    p["email"].strip()
                ):
                    raise Invalid("country/email do not satisfy the Central billing profile rules")
                rate = D(p["vat_rate"])
                if rate != rate.quantize(Q4) or not 0 <= rate <= 1:
                    raise Invalid("vat_rate must be 0..1 with at most 4 decimals")
                item = items.get(("profiles", sid))
                cur = db.scalar(
                    select(BillingProfile).where(BillingProfile.enterprise_id == uuid.UUID(eid))
                )
                if item is not None:
                    if item.source_hash != d.core_hash:
                        d.classification, d.reason = (
                            "conflict",
                            "profile identity changed at the source",
                        )
                    elif item.state_hash == d.state_hash:
                        d.classification = "already_imported"
                    elif authority == "central":
                        d.classification, d.reason = (
                            "conflict",
                            "billing profile is owned by Central after the cutover",
                        )
                    else:
                        d.action = "advance"
                elif cur is not None:
                    same = (cur.legal_name, cur.address, cur.country, cur.tax_id, cur.email.strip(), D(cur.vat_rate)) == (
                        p["legal_name"].strip(), p["address"].strip(), p["country"].upper(), p["tax_id"], p["email"].strip(), rate)  # fmt: skip
                    if same:
                        d.classification, d.action = "exact", "link"
                    else:
                        d.classification, d.reason = (
                            "conflict",
                            "a different Central billing profile exists for this enterprise",
                        )
                else:
                    d.action = "create"
        except Invalid as e:
            d.classification, d.reason = "invalid", str(e)
        out[eid or f"none:{sid}"] = d
        plan.decisions.append(d)
    return out


def _subscriptions(
    db, doc, items, enterprises, d_plans, d_profiles, authority, plan
) -> dict[int, Decision]:
    out: dict[int, Decision] = {}
    plan_by_src = {p["source_id"]: p for p in doc["plans"]}
    for s in doc["subscriptions"]:
        sid = str(s["source_id"])
        state = {
            k: s[k]
            for k in (
                "plan_source_id",
                "pending_plan_source_id",
                "status",
                "started_at",
                "periods_billed",
                "cancel_at_period_end",
            )
        }
        d = Decision(
            "subscriptions",
            sid,
            "importable",
            core_hash=h({"enterprise_id": s["enterprise_id"], "owner_ref": s["owner_ref"]}),
            state_hash=h(state),
            data=s,
        )
        eid = s["enterprise_id"]
        pd = d_plans.get(s["plan_source_id"])
        pend = (
            d_plans.get(s["pending_plan_source_id"])
            if s["pending_plan_source_id"] is not None
            else None
        )
        if eid is None:
            d.classification, d.reason = (
                "invalid",
                "subscription has no enterprise_id (cannot be mapped to Central)",
            )
        elif uuid.UUID(eid) not in enterprises:
            d.classification, d.reason = (
                "requires_manual_review",
                "enterprise does not exist in Central",
            )
        elif (
            pd is None
            or pd.blocked
            or (s["pending_plan_source_id"] is not None and (pend is None or pend.blocked))
        ):
            d.classification, d.reason = (
                "requires_manual_review",
                "the subscription's plan is missing or blocked",
            )
        elif (
            s["pending_plan_source_id"] is not None
            and plan_by_src[s["pending_plan_source_id"]]["currency"]
            != plan_by_src[s["plan_source_id"]]["currency"]
        ):
            d.classification, d.reason = (
                "requires_manual_review",
                "pending plan has another currency (no FX)",
            )
        elif d_profiles.get(eid) is None or d_profiles[eid].blocked:
            d.classification, d.reason = (
                "requires_manual_review",
                "no importable billing profile for the enterprise",
            )
        else:
            item = items.get(("subscriptions", sid))
            cur = db.scalar(
                select(BillingSubscription).where(
                    BillingSubscription.enterprise_id == uuid.UUID(eid)
                )
            )
            if item is not None:
                if item.source_hash != d.core_hash:
                    d.classification, d.reason = (
                        "conflict",
                        "subscription identity changed at the source",
                    )
                elif item.state_hash == d.state_hash:
                    d.classification = "already_imported"
                elif authority == "central":
                    d.classification, d.reason = (
                        "conflict",
                        "subscription is owned by Central after the cutover",
                    )
                else:
                    sub = db.get(BillingSubscription, item.target_id)
                    if sub is None or billing.utc(sub.anchor_started_at) != T(s["started_at"]):
                        d.classification, d.reason = (
                            "requires_manual_review",
                            "re-anchored (reactivated) subscription after import needs manual review",
                        )
                    elif s["periods_billed"] < sub.next_period_index:
                        d.classification, d.reason = "conflict", "periods_billed went backwards"
                    else:
                        d.action = "advance"
            elif cur is not None:
                d.classification, d.reason = (
                    "conflict",
                    "a Central subscription already exists for this enterprise",
                )
            else:
                d.action = "create"
        out[s["source_id"]] = d
        plan.decisions.append(d)
    return out


def _cents(x: Decimal) -> Decimal:
    return billing.cents(x)


def _invoice_arith(inv: dict) -> str | None:
    if not inv["lines"]:
        return "invoice has no lines"
    total = Decimal(0)
    for ln in inv["lines"]:
        q, u, a = D(ln["quantity"]), D(ln["unit_price"]), D(ln["amount"])
        if q <= 0 or u < 0 or a < 0:
            return "line quantity/price/amount outside the Central limits"
        if a != _cents(q * u):
            return "line amount != cents(quantity*unit_price)"
        total += a
    rate = D(inv["vat_rate"])
    if rate != rate.quantize(Q4) or not 0 <= rate <= 1:
        return "vat_rate outside 0..1 or with more than 4 decimals"
    if (
        D(inv["subtotal"]) != total
        or D(inv["tax"]) != _cents(total * rate)
        or D(inv["total"]) != D(inv["subtotal"]) + D(inv["tax"])
    ):
        return "subtotal/tax/total do not follow from the lines and vat_rate"
    return None


def _invoice_period_index(sub: dict, inv: dict) -> int:
    """k nëse `period_start` përputhet me ankorën aktuale në [0, periods_billed); përndryshe segment i mëparshëm ⇒ −source_id."""
    start = T(sub["started_at"])
    for k in range(0, sub["periods_billed"]):
        if T(inv["period_start"]) == billing.add_months(start, k) and T(
            inv["period_end"]
        ) == billing.add_months(start, k + 1):
            return k
    return -inv["source_id"]


def _invoices(db, doc, items, d_subs, d_plans, authority, plan) -> None:
    subs = {s["source_id"]: s for s in doc["subscriptions"]}
    pays_by_inv: dict[int, list[dict]] = {}
    for p in doc["payments"]:
        pays_by_inv.setdefault(p["invoice_source_id"], []).append(p)
    wallet = {w["invoice_source_id"]: w for w in doc["wallet_settlements"]}
    existing_numbers = {n: i for n, i in db.execute(select(Invoice.number, Invoice.id))}
    for inv in doc["invoices"]:
        sid = str(inv["source_id"])
        core = {k: inv[k] for k in ("number", "enterprise_id", "subscription_source_id", "period_start", "period_end", "currency", "subtotal", "vat_rate", "tax", "total",
                                    "bill_to", "issued_at", "due_at")} | {"lines": inv["lines"]}  # fmt: skip
        state = {
            "status": inv["status"],
            "paid_at": inv["paid_at"],
            "paid_via": inv["paid_via"],
            "voided_reason": inv["voided_reason"],
        }
        pays = pays_by_inv.get(inv["source_id"], [])
        d = Decision(
            "invoices",
            sid,
            "importable",
            core_hash=h(core),
            state_hash=h(
                state | {"p": [p["source_id"] for p in pays if p["status"] == "succeeded"]}
            ),
            data=inv,
        )
        sd = (
            d_subs.get(inv["subscription_source_id"])
            if inv["subscription_source_id"] is not None
            else None
        )
        item = items.get(("invoices", sid))
        try:
            if inv["enterprise_id"] is None:
                d.classification, d.reason = "invalid", "invoice has no enterprise_id"
            elif sd is None or sd.blocked:
                d.classification, d.reason = (
                    "requires_manual_review",
                    "the invoice's subscription is missing or blocked",
                )
            elif sd.data["enterprise_id"] != inv["enterprise_id"]:
                d.classification, d.reason = (
                    "invalid",
                    "invoice and subscription belong to different enterprises",
                )
            elif not _INV_NUM.match(inv["number"]):
                d.classification, d.reason = (
                    "requires_manual_review",
                    "invoice number does not follow INV-YYYY-NNNNNN (cannot seed the sequence safely)",
                )
            elif (
                not _CURRENCY.match(inv["currency"])
                or inv["currency"] != d_plans[sd.data["plan_source_id"]].data["currency"]
            ):
                d.classification, d.reason = (
                    "requires_manual_review",
                    "invoice currency differs from the subscription plan currency",
                )
            elif T(inv["period_end"]) <= T(inv["period_start"]):
                d.classification, d.reason = "invalid", "period_end <= period_start"
            elif (why := _invoice_arith(inv)) is not None:
                d.classification, d.reason = "invalid", why
            else:
                try:
                    bt = json.loads(inv["bill_to"])
                    if not isinstance(bt, dict):
                        raise ValueError
                except ValueError:
                    d.classification, d.reason = "invalid", "bill_to is not a JSON object"
                    bt = None
                if bt is not None:
                    d.data = {
                        **inv,
                        "bill_to_obj": bt,
                        "period_index": _invoice_period_index(
                            subs[inv["subscription_source_id"]], inv
                        ),
                    }
                    _settle(d, inv, pays, wallet.get(inv["source_id"]))
                    if not d.blocked:
                        if item is not None:
                            if item.source_hash != d.core_hash:
                                d.classification, d.reason = (
                                    "conflict",
                                    "invoice content changed at the source after import (issued invoices are immutable)",
                                )
                            elif item.state_hash == d.state_hash:
                                d.classification = "already_imported"
                            elif authority == "central":
                                d.classification, d.reason = (
                                    "conflict",
                                    "invoice settlement is owned by Central after the cutover",
                                )
                            else:
                                cur = db.get(Invoice, item.target_id)
                                if (
                                    cur is not None
                                    and cur.status == INV_OPEN
                                    and inv["status"] in ("paid", "void")
                                ):
                                    d.action = "advance"
                                else:
                                    d.classification, d.reason = (
                                        "conflict",
                                        "unsupported invoice state transition",
                                    )
                        elif inv["number"] in existing_numbers:
                            d.classification, d.reason = (
                                "conflict",
                                "an invoice with this number already exists in Central",
                            )
                        else:
                            d.action = "create"
        except KeyError:
            d.classification, d.reason = (
                "requires_manual_review",
                "inconsistent references in the artifact",
            )
        plan.decisions.append(d)
        for p in pays:
            if p["status"] != "succeeded":
                pd = Decision(
                    "payments",
                    str(p["source_id"]),
                    "exact",
                    reason="non-succeeded legacy checkout session is not imported (expired/failed/pending)",
                )
            elif d.blocked:
                pd = Decision(
                    "payments",
                    str(p["source_id"]),
                    "exact",
                    reason="follows its blocked invoice (see the invoice issue)",
                )
            else:
                pd = Decision(
                    "payments",
                    str(p["source_id"]),
                    "already_imported" if d.classification == "already_imported" else "importable",
                )
            plan.decisions.append(pd)


def _gap(paid: Decimal, paid_cur: str, total: Decimal, cur: str) -> str:
    """Fjali e qartë për operatorin: monedhë ndryshe / pjesore / mbipagesë (kategoritë e workflow-t manual)."""
    if paid_cur != cur:
        return "has another currency than"
    return (
        "is lower than (partial payment of)" if paid < total else "is higher than (overpayment of)"
    )


def _settle(d: Decision, inv: dict, pays: list[dict], wallet: dict | None) -> None:
    """Prova e shlyerjes për fatura `paid`; pagesa të sukseshme mbi fatura jo-të-paguara bllokohen (nuk shpikim kurrë)."""
    succ = [p for p in pays if p["status"] == "succeeded"]
    d.data["settlement"] = None
    if inv["status"] != "paid":
        if succ:
            d.classification, d.reason = (
                "requires_manual_review",
                "a succeeded payment exists on an invoice that is not paid at the source",
            )
        elif inv["status"] == "void" and not inv["voided_reason"]:
            d.data["voided_reason_default"] = True
        return
    if inv["paid_at"] is None:
        d.classification, d.reason = "requires_manual_review", "paid invoice without paid_at"
        return
    total, cur = D(inv["total"]), inv["currency"]
    if inv["paid_via"] == "wallet":
        if succ:
            d.classification, d.reason = (
                "requires_manual_review",
                "wallet-paid invoice also has a succeeded online payment",
            )
        elif wallet is None:
            d.classification, d.reason = (
                "requires_manual_review",
                "wallet-paid invoice without wallet ledger evidence",
            )
        elif D(wallet["amount"]) != total or wallet["currency"] != cur:
            d.classification, d.reason = (
                "unsupported",
                f"wallet debit {_gap(D(wallet['amount']), wallet['currency'], total, cur)} the invoice total",
            )
        else:
            d.data["settlement"] = {
                "kind": "wallet",
                "ledger_entry_id": wallet["ledger_entry_id"],
                "at": wallet["created_at"],
                "amount": wallet["amount"],
            }
    elif inv["paid_via"] == "online":
        if len(succ) != 1:
            d.classification, d.reason = (
                "unsupported",
                f"{len(succ)} succeeded online payments (V1 imports exactly one full payment)",
            )
        elif D(succ[0]["amount"]) != total or succ[0]["currency"] != cur:
            d.classification, d.reason = (
                "unsupported",
                f"online payment {_gap(D(succ[0]['amount']), succ[0]['currency'], total, cur)} the invoice total",
            )
        else:
            p = succ[0]
            ref = f"{p['provider']}:{p['external_id']}"
            if len(ref) > 128:
                d.classification, d.reason = "invalid", "payment reference too long"
            else:
                d.data["settlement"] = {
                    "kind": "online",
                    "payment_source_id": p["source_id"],
                    "ref": ref,
                    "at": p["completed_at"] or inv["paid_at"],
                    "amount": p["amount"],
                }
    else:
        d.classification, d.reason = (
            "requires_manual_review",
            "paid invoice with unknown settlement channel (paid_via)",
        )


def _sequences(db, doc, plan) -> None:
    maxima: dict[int, int] = {}
    for c in doc["sequences"]["invoice_counters"]:
        maxima[c["year"]] = max(maxima.get(c["year"], 0), c["last_number"])
    for inv in doc["invoices"]:  # TË GJITHA numrat legacy (edhe të bllokuarit) përfshihen në seed
        m = _INV_NUM.match(inv["number"])
        if m:
            maxima[int(m.group(1))] = max(maxima.get(int(m.group(1)), 0), int(m.group(2)))
    for y, n in maxima.items():
        cur = (
            db.scalar(
                select(InvoiceNumberSequence.last_number).where(InvoiceNumberSequence.year == y)
            )
            or 0
        )
        plan.seeds[y] = max(int(cur), n)
    if doc["sequences"]["credit_note_like"] > 0:
        plan.decisions.append(
            Decision(
                "credit_note_like",
                "all",
                "requires_manual_review",
                reason=f"{doc['sequences']['credit_note_like']} legacy credit-note-like documents need an audit before seeding",
            )
        )
    else:
        plan.notes.append(
            "no legacy credit-note numbering exists (schema audit): credit_note_sequence is not seeded"
        )


def _baselines(db, doc, items, enterprises, d_subs, plan) -> None:
    active = {
        s["enterprise_id"]
        for s in doc["subscriptions"]
        if s["status"] == "active" and s["enterprise_id"]
    }
    by_ent_ok = {d.data["enterprise_id"] for d in d_subs.values() if not d.blocked}
    for u in doc["usage"]:
        # ≤ 64 shenja (kolona source_id): UUID pa viza + epoch i kufirit (g5: çelësi i vjetër 69 shenja thyente çështjet në PG)
        key = f"{uuid.UUID(u['enterprise_id']).hex}:{int(T(u['boundary']).timestamp())}"
        d = Decision("usage_baselines", key, "importable", core_hash=h(u), data=u)
        eid = u["enterprise_id"]
        if eid not in active:
            d.classification, d.reason = (
                "exact",
                "enterprise has no active legacy subscription: no baseline needed",
            )
        elif uuid.UUID(eid) not in enterprises or eid not in by_ent_ok:
            d.classification, d.reason = (
                "requires_manual_review",
                "enterprise/subscription is not importable",
            )
        elif u["product_id"] is None:
            d.classification, d.reason = (
                "requires_manual_review",
                "no single email product entitlement at the source",
            )
        elif (
            db.scalar(
                select(Product.id).where(
                    Product.id == uuid.UUID(u["product_id"]), Product.channel == "email"
                )
            )
            is None
        ):
            d.classification, d.reason = (
                "requires_manual_review",
                "the email product does not exist in Central",
            )
        elif u["capture_active_since"] is None or T(u["capture_active_since"]) > T(u["boundary"]):
            d.classification, d.reason = (
                "requires_manual_review",
                "billable-event capture started after the first Central period: usage evidence is incomplete (wait for the next period boundary and re-export)",
            )
        else:
            cur = db.scalar(select(BillingUsageBaseline).where(BillingUsageBaseline.enterprise_id == uuid.UUID(eid), BillingUsageBaseline.product_id == uuid.UUID(u["product_id"]),
                                                              BillingUsageBaseline.boundary == T(u["boundary"])))  # fmt: skip
            if cur is not None:
                same = (cur.cumulative_count, cur.watermark) == (
                    u["cumulative_before_boundary"],
                    u["watermark_before_boundary"],
                )
                d.classification, d.reason = (
                    ("already_imported", "")
                    if same
                    else ("conflict", "baseline for this boundary already exists with other values")
                )
            else:
                d.action = "create"
        plan.baselines.append(d)


# --- aplikimi (shkrim; një transaksion; i thirrësit commit-on) ----------------------------------------------------------------------------


def _record_item(
    db, d: Decision, table: str, target_type: str, target_id, batch, now, detail: dict | None = None
) -> None:
    item = db.scalar(select(BillingImportItem).where(BillingImportItem.source_system == SOURCE, BillingImportItem.source_table == table,
                                                      BillingImportItem.source_id == d.source_id))  # fmt: skip
    if item is None:
        db.add(BillingImportItem(source_system=SOURCE, source_table=table, source_id=d.source_id, source_hash=d.core_hash, state_hash=d.state_hash,
                                 target_type=target_type, target_id=target_id, batch_id=batch.id, last_batch_id=batch.id, imported_at=now, updated_at=now,
                                 detail=detail or {}))  # fmt: skip
    else:
        item.state_hash, item.last_batch_id, item.updated_at = d.state_hash, batch.id, now
        if detail:
            item.detail = {**item.detail, **detail}
    db.flush()


def _target(db, table: str, source_id) -> uuid.UUID | None:
    it = db.scalar(select(BillingImportItem).where(BillingImportItem.source_system == SOURCE, BillingImportItem.source_table == table,
                                                    BillingImportItem.source_id == str(source_id)))  # fmt: skip
    return None if it is None else it.target_id


def apply(
    db: Session,
    doc: dict,
    actor,
    evidence_hash: str,
    *,
    authority: str = "local",
    require_clean: bool = False,
    now: datetime | None = None,
) -> BillingImportBatch:
    """Aplikon artifact-in e verifikuar. `evidence_hash` duhet të përputhet me `content_hash` (hash-i i dry-run). Rirunim i të njëjtit artifact ⇒ batch-i ekzistues (no-op)."""
    actor = money_common.admin(actor)
    actor_id = actor.id
    try:
        lx.parse(doc)
    except lx.ContractError as e:
        raise Invalid(f"invalid export artifact: {e}") from e
    if evidence_hash != doc["content_hash"]:
        raise Conflict(
            "evidence hash does not match the artifact content hash (hash mismatch: refusing to apply)"
        )
    prior = db.scalar(
        select(BillingImportBatch).where(
            BillingImportBatch.export_id == uuid.UUID(doc["export_id"])
        )
    )
    if prior is not None:
        if prior.content_hash != doc["content_hash"]:
            raise Conflict(
                "export_id was already imported with a different content hash (a modified artifact needs a new export_id)"
            )
        return prior
    plan = plan_import(db, doc, authority=authority)
    if require_clean and plan.blocking():
        raise Conflict(
            f"{len(plan.blocking())} blocking row(s): resolve them or apply without --require-clean"
        )
    now = billing.utc(now or utcnow())
    batch = BillingImportBatch(export_id=uuid.UUID(doc["export_id"]), content_hash=doc["content_hash"], generated_at=T(doc["generated_at"]),
                               attestation=doc["authority"], summary={"classification": plan.summary(), "sequence_seeds": {str(y): n for y, n in sorted(plan.seeds.items())}, "blocking": len(plan.blocking())}, applied_by_id=actor_id, applied_at=now)  # fmt: skip
    db.add(batch)
    db.flush()
    plan_ver: dict[int, uuid.UUID] = {}
    counts = {"created": 0, "advanced": 0, "linked": 0}
    for d in plan.by("plans"):
        _apply_plan(db, d, actor, batch, now, plan_ver, counts)
    for d in plan.by("profiles"):
        _apply_profile(db, d, actor, batch, now, counts)
    sub_map: dict[int, uuid.UUID] = {}
    for d in plan.by("subscriptions"):
        _apply_subscription(db, d, plan_ver, batch, now, sub_map, counts)
    for d in plan.baselines:
        _apply_baseline(db, d, actor, batch, now, counts)
    for d in plan.by("invoices"):
        _apply_invoice(db, d, actor, batch, now, counts)
    _apply_seeds(db, plan.seeds, actor, now)
    for d in plan.decisions + plan.baselines:
        if d.blocked:
            db.add(
                BillingImportIssue(
                    batch_id=batch.id,
                    source_table=d.table,
                    source_id=d.source_id,
                    classification=d.classification,
                    reason=d.reason[:300],
                    created_at=now,
                )
            )
    db.flush()
    batch_summary = (
        batch.summary
    )  # i pandryshueshëm: llogaritet para INSERT (sekuenca/klasifikimi njihen nga plani)
    audit.record(db, actor, "billing.import_apply", "billing_import_batch", batch.id,
                 {"export_id": doc["export_id"], "content_hash": doc["content_hash"], "applied": counts, "blocking": len(plan.blocking()), "sequence_seeds": batch_summary["sequence_seeds"],
                  "attestation": doc["authority"]}, now=now)  # fmt: skip
    audit.record_system(
        db,
        label=SYSTEM,
        action="billing.import_items",
        resource_type="billing_import_batch",
        resource_id=batch.id,
        detail={"classification": plan.summary()},
        now=now,
    )
    db.refresh(batch)
    return batch


def _apply_plan(db, d: Decision, actor, batch, now, plan_ver, counts) -> None:
    p = d.data
    if d.classification == "already_imported" and d.action == "none":
        plan_ver[int(d.source_id)] = _target(db, "plans", d.source_id)
        return
    if d.blocked:
        return
    if d.action == "link":
        vid = uuid.UUID(p["existing_version_id"])
        plan_ver[int(d.source_id)] = vid
        _record_item(db, d, "plans", "plan_version", vid, batch, now, {"linked": True})
        counts["linked"] += 1
        return
    if d.action == "create":
        cp = billing_plans.create_plan(db, actor, p["code"], p["name"], now=now)
        v = billing_plans.new_version(
            db, actor, cp.id, p["currency"], p["monthly_fee"], p["included_emails"], now=now
        )
        billing_plans.activate(db, actor, v.id, now=now)
        if p["status"] == "retired":
            billing_plans.retire(db, actor, v.id, "legacy plan was retired at the source", now=now)
        plan_ver[int(d.source_id)] = v.id
        _record_item(
            db,
            d,
            "plans",
            "plan_version",
            v.id,
            batch,
            now,
            {"legacy_email_overage_price": p["email_overage_price"], "source_code": p["code"]},
        )
        counts["created"] += 1
    elif d.action == "advance":
        vid = _target(db, "plans", d.source_id)
        v = db.get(PlanVersion, vid)
        if v.status != V_RETIRED:
            billing_plans.retire(db, actor, vid, "legacy plan was retired at the source", now=now)
        plan_ver[int(d.source_id)] = vid
        _record_item(db, d, "plans", "plan_version", vid, batch, now)
        counts["advanced"] += 1
    else:
        plan_ver[int(d.source_id)] = _target(db, "plans", d.source_id)


def _apply_profile(db, d: Decision, actor, batch, now, counts) -> None:
    if d.blocked or d.action == "none":
        return
    p = d.data
    eid = uuid.UUID(p["enterprise_id"])
    prof = billing.set_profile(
        db,
        actor,
        eid,
        p["legal_name"],
        p["address"],
        p["country"],
        p["email"],
        p["tax_id"],
        D(p["vat_rate"]).quantize(Q4),
        now=now,
    )
    _record_item(db, d, "profiles", "billing_profile", prof.id, batch, now)
    counts[
        "linked" if d.action == "link" else ("advanced" if d.action == "advance" else "created")
    ] += 1


def _apply_subscription(db, d: Decision, plan_ver, batch, now, sub_map, counts) -> None:
    s = d.data
    if d.classification == "already_imported":
        sub_map[int(d.source_id)] = _target(db, "subscriptions", d.source_id)
        return
    if d.blocked:
        return
    pv = plan_ver[s["plan_source_id"]]
    pend = (
        plan_ver[s["pending_plan_source_id"]] if s["pending_plan_source_id"] is not None else None
    )
    if pend == pv:
        pend = None
    if d.action == "create":
        sub = BillingSubscription(enterprise_id=uuid.UUID(s["enterprise_id"]), plan_version_id=pv, pending_plan_version_id=pend, status=SUB_ACTIVE if s["status"] == "active" else SUB_CANCELLED,
                                  cancel_at_period_end=s["cancel_at_period_end"], anchor_started_at=T(s["started_at"]), anchor_period_index=0, next_period_index=s["periods_billed"],
                                  cancelled_at=None if s["status"] == "active" else now, created_at=now, updated_at=now)  # fmt: skip
        db.add(sub)
        db.flush()
        detail = {"cancelled_at_provenance": "import_time"} if s["status"] == "cancelled" else {}
        _record_item(db, d, "subscriptions", "billing_subscription", sub.id, batch, now, detail)
        counts["created"] += 1
    else:  # advance
        sub = db.get(BillingSubscription, _target(db, "subscriptions", d.source_id))
        sub.plan_version_id, sub.pending_plan_version_id = pv, pend
        sub.cancel_at_period_end, sub.next_period_index, sub.updated_at = (
            s["cancel_at_period_end"],
            s["periods_billed"],
            now,
        )
        if s["status"] == "cancelled" and sub.status == SUB_ACTIVE:
            sub.status, sub.cancelled_at = SUB_CANCELLED, now
        db.flush()
        _record_item(db, d, "subscriptions", "billing_subscription", sub.id, batch, now)
        counts["advanced"] += 1
    sub_map[int(d.source_id)] = sub.id


def _apply_baseline(db, d: Decision, actor, batch, now, counts) -> None:
    if d.blocked or d.action != "create":
        return
    u = d.data
    row = BillingUsageBaseline(enterprise_id=uuid.UUID(u["enterprise_id"]), product_id=uuid.UUID(u["product_id"]), boundary=T(u["boundary"]), cumulative_count=u["cumulative_before_boundary"],
                               watermark=u["watermark_before_boundary"], capture_active_since=T(u["capture_active_since"]), source_batch_id=batch.id, source_hash=d.core_hash,
                               created_by_id=actor.id, created_at=now)  # fmt: skip
    db.add(row)
    db.flush()
    audit.record(db, actor, "billing.baseline_create", "billing_usage_baseline", row.id,
                 {"enterprise_id": u["enterprise_id"], "boundary": u["boundary"], "cumulative_count": row.cumulative_count, "watermark": row.watermark}, now=now)  # fmt: skip
    counts["created"] += 1


def _apply_invoice(db, d: Decision, actor, batch, now, counts) -> None:
    if d.blocked or d.classification == "already_imported":
        return
    inv = d.data
    if d.action == "advance":
        row = db.get(Invoice, _target(db, "invoices", d.source_id))
        _settle_or_void(db, row, inv, actor, now, batch)
        _record_item(db, d, "invoices", "invoice", row.id, batch, now)
        counts["advanced"] += 1
        return
    sub_id = _target(db, "subscriptions", inv["subscription_source_id"])
    sub = db.get(BillingSubscription, sub_id)
    reason = inv["voided_reason"] or VOID_DEFAULT_REASON
    detail = {
        "issuer": "unknown",
        "plan_version": "unknown",
        "period_index": "legacy_segment" if inv["period_index"] < 0 else "derived_from_anchor",
    }
    row = Invoice(number=inv["number"], enterprise_id=sub.enterprise_id, subscription_id=sub.id, period_index=inv["period_index"], period_start=T(inv["period_start"]), period_end=T(inv["period_end"]),
                  plan_version_id=None, provenance=PROV_LEGACY, currency=inv["currency"], subtotal=D(inv["subtotal"]), vat_rate=D(inv["vat_rate"]), tax=D(inv["tax"]), total=D(inv["total"]),
                  status=INV_OPEN, bill_to=inv["bill_to_obj"], issuer={"provenance": "unknown"}, issued_at=T(inv["issued_at"]), due_at=T(inv["due_at"]), created_at=now)  # fmt: skip
    if inv["status"] == "paid":
        row.status, row.paid_at = INV_PAID, T(inv["paid_at"])
    elif inv["status"] == "void":
        row.status, row.voided_at, row.voided_by_id, row.voided_reason = (
            INV_VOID,
            now,
            actor.id,
            reason[:500],
        )
        detail["voided_at"] = "import_time"
        if inv.get("voided_reason_default"):
            detail["voided_reason"] = "default"
    db.add(row)
    db.flush()
    for i, ln in enumerate(inv["lines"], 1):
        src = ln["pricing_source"]
        pvr = uuid.UUID(ln["pricing_version_ref"]) if ln["pricing_version_ref"] else None
        known = pvr is not None and db.get(PriceVersion, pvr) is not None
        db.add(InvoiceLine(invoice_id=row.id, currency=row.currency, line_no=i, line_type=line_type(ln["description"], D(ln["quantity"])), description=ln["description"][:200], quantity=D(ln["quantity"]),
                           unit_price=D(ln["unit_price"]), amount=D(ln["amount"]), period_start=row.period_start, period_end=row.period_end, plan_version_id=None, pricing_source=src,
                           price_version_id=pvr if known else None))  # fmt: skip
    db.flush()
    if inv["status"] == "paid":
        pay = _allocate(db, row, inv["settlement"], actor, now)
        _record_payment(db, inv, pay, batch, now)
    if 0 <= inv["period_index"] < sub.next_period_index:
        db.add(BillingPeriod(subscription_id=sub.id, enterprise_id=sub.enterprise_id, period_index=inv["period_index"], period_start=row.period_start, period_end=row.period_end,
                             plan_version_id=None, provenance=PROV_LEGACY, status=P_INVOICED, invoice_id=row.id, billed_at=row.issued_at, created_at=now))  # fmt: skip
    db.flush()
    _record_item(db, d, "invoices", "invoice", row.id, batch, now, detail)
    counts["created"] += 1


def _allocate(db, row: Invoice, st: dict, actor, now) -> None:
    ref = st["ref"] if st["kind"] == "online" else f"wallet-ledger:{st['ledger_entry_id']}"
    at = T(st["at"])
    note = (
        "legacy online payment"
        if st["kind"] == "online"
        else "legacy wallet settlement (historical; the wallet debit is NOT replayed)"
    )
    pay = Payment(enterprise_id=row.enterprise_id, purpose=PURPOSE_INVOICE, invoice_id=row.id, account_id=None, currency=row.currency, amount=D(st["amount"]), source="legacy_import",
                  external_reference=money_common.external_reference(ref), note=note, status=APPROVED, created_by_label=SYSTEM, approved_at=at, approved_by_id=actor.id, created_at=now, updated_at=now)  # fmt: skip
    db.add(pay)
    db.flush()
    db.add(
        InvoicePaymentAllocation(
            payment_id=pay.id,
            invoice_id=row.id,
            enterprise_id=row.enterprise_id,
            amount=pay.amount,
            currency=row.currency,
            allocated_at=at,
            allocated_by_id=actor.id,
        )
    )
    db.flush()
    return pay


def _settle_or_void(db, row: Invoice, inv: dict, actor, now, batch=None) -> None:
    if inv["status"] == "paid":
        row.status, row.paid_at = INV_PAID, T(inv["paid_at"])
        db.flush()
        pay = _allocate(db, row, inv["settlement"], actor, now)
        if batch is not None:
            _record_payment(db, inv, pay, batch, now)
    else:
        row.status, row.voided_at, row.voided_by_id = INV_VOID, now, actor.id
        row.voided_reason = (inv["voided_reason"] or VOID_DEFAULT_REASON)[:500]
        db.flush()


def _apply_seeds(db, seeds: dict[int, int], actor, now) -> dict[int, int]:
    """Ngre (kurrë nuk ul) `invoice_number_sequence` mbi maksimumin legacy/Central; i audituar; rresht i kyçur."""
    done: dict[int, int] = {}
    for year, n in sorted(seeds.items()):
        row = db.scalar(
            select(InvoiceNumberSequence)
            .where(InvoiceNumberSequence.year == year)
            .with_for_update()
        )
        if row is None:
            db.add(InvoiceNumberSequence(year=year, last_number=n))
            before = 0
        else:
            before = row.last_number
            if n > row.last_number:
                row.last_number = n
        db.flush()
        if n > before:
            audit.record(
                db,
                actor,
                "billing.sequence_seed",
                "invoice_number_sequence",
                str(year),
                {"year": year, "before": before, "after": n},
                now=now,
            )
        done[year] = max(n, before)
    return done


# --- workflow i çështjeve manuale (M9-g5) -------------------------------------------------------------------------------------------------------
# Kategori → çfarë shikon operatori / veprimi i lejuar / shkurtorja e ndaluar. Një çështje NUK mbyllet me "u zgjidh" pa veprim real:
# (1) `superseded`: një batch i mëvonshëm e ka importuar objektin ose e mban si çështje më të re (zgjidhja e vërtetë ndodh në burim + ri-eksport);
# (2) `operator_waiver`: vetëm `requires_manual_review` (jo baseline), me `evidence_ref` (tiketë/dokument) — objekti MBETET jashtë Central dhe numërohet WARN.
# `unsupported | invalid | conflict` nuk heqen kurrë me fjalë: shërbejnë vetëm si `superseded`.
ISSUE_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("partial_payment", r"partial payment"),
    ("overpayment", r"overpayment"),
    ("currency_mismatch", r"another currency|currency differs"),
    (
        "usage_baseline_insufficient",
        r"usage evidence is incomplete|email product|entitlement|baseline",
    ),
    ("sequence_conflict", r"INV-YYYY|number already exists|sequence"),
    ("invoice_arithmetic_mismatch", r"arithmetic|subtotal|tax/total|period_end <= period_start"),
    (
        "missing_external_reference",
        r"paid_via|without wallet|without paid_at|payment reference|succeeded online payments|succeeded payment",
    ),
    ("unsupported_line", r"line"),
    ("plan_conflict", r"plan"),
    ("subscription_conflict", r"subscription|periods_billed|re-anchored"),
)
ISSUE_POLICY: dict[str, dict[str, str]] = {
    "partial_payment": {
        "allowed": "fix the settlement at the source (one full payment) and re-export; or void/credit it in Enterprise before cutover",
        "forbidden": "importing a partial payment as paid; resolving by text",
    },
    "overpayment": {
        "allowed": "refund/correct at the source and re-export",
        "forbidden": "importing the surplus; resolving by text",
    },
    "missing_external_reference": {
        "allowed": "supply the real payment reference at the source and re-export; or waive WITH evidence_ref (invoice stays outside Central)",
        "forbidden": "inventing a reference; marking paid without evidence",
    },
    "usage_baseline_insufficient": {
        "allowed": "wait for the next period boundary and re-export (capture must predate the first Central period)",
        "forbidden": "waiver; zero baseline; backdating capture_active_since",
    },
    "unsupported_line": {
        "allowed": "correct the source line text/arithmetic and re-export (unknown lines import as `legacy` only when arithmetic is exact)",
        "forbidden": "editing amounts in Central",
    },
    "plan_conflict": {
        "allowed": "restore the source plan (plans are immutable; new price = new code) and re-export",
        "forbidden": "editing Central plan versions to match",
    },
    "subscription_conflict": {
        "allowed": "align periods_billed/anchor at the source and re-export; re-anchored subscriptions need a reviewed manual mapping",
        "forbidden": "resetting next_period_index in Central",
    },
    "sequence_conflict": {
        "allowed": "fix the number at the source or remove the colliding Central invoice through the normal void flow, then re-export",
        "forbidden": "lowering invoice_number_sequence",
    },
    "invoice_arithmetic_mismatch": {
        "allowed": "correct the source invoice (credit note flow) and re-export",
        "forbidden": "recomputing totals during import",
    },
    "currency_mismatch": {
        "allowed": "align the plan/invoice currency at the source (no FX in V1) and re-export",
        "forbidden": "converting currency",
    },
    "other": {
        "allowed": "investigate; fix at the source and re-export",
        "forbidden": "waiver of invalid/unsupported/conflict rows",
    },
}
_CAT_RX = tuple((c, re.compile(rx, re.I)) for c, rx in ISSUE_CATEGORIES)
WAIVER_REF_MIN, WAIVER_REF_MAX = 8, 200
_KIND = re.compile(r"^kind=(superseded|operator_waiver)(?:; ref=([^;]{8,200}))?; (.+)$", re.S)


def categorize(reason: str) -> str:
    for cat, rx in _CAT_RX:
        if rx.search(reason or ""):
            return cat
    return "other"


def issue_view(row: BillingImportIssue) -> dict:
    """Ç'shikon operatori (pa PII): kategori, klasë, burim, arsye, veprim i lejuar/i ndaluar, a mund të heqet me waiver."""
    cat = categorize(row.reason)
    pol = ISSUE_POLICY.get(cat, ISSUE_POLICY["other"])
    return {"id": str(row.id), "category": cat, "classification": row.classification, "source_table": row.source_table, "source_id": row.source_id,
            "reason": row.reason, "allowed": pol["allowed"], "forbidden": pol["forbidden"], "waivable": _waivable(row, cat), "resolved": row.resolved_at is not None}  # fmt: skip


def _waivable(row: BillingImportIssue, cat: str) -> bool:
    return row.classification == "requires_manual_review" and cat != "usage_baseline_insufficient"


def _superseded(db: Session, row: BillingImportIssue) -> bool:
    """Batch i mëvonshëm ekziston DHE (objekti është importuar prej tij ose një çështje më e re e mban të njëjtin objekt)."""
    cur = db.get(BillingImportBatch, row.batch_id)
    later = db.scalars(select(BillingImportBatch.id).where(BillingImportBatch.applied_at > cur.applied_at, BillingImportBatch.id != cur.id)).all()  # fmt: skip
    if not later:
        return False
    item = db.scalar(select(BillingImportItem).where(BillingImportItem.source_system == SOURCE, BillingImportItem.source_table == row.source_table,
                                                     BillingImportItem.source_id == row.source_id, BillingImportItem.last_batch_id.in_(later)))  # fmt: skip
    newer = db.scalar(select(BillingImportIssue.id).where(BillingImportIssue.batch_id.in_(later), BillingImportIssue.source_table == row.source_table,
                                                          BillingImportIssue.source_id == row.source_id))  # fmt: skip
    return item is not None or newer is not None


def resolve_issue(
    db: Session, actor, issue_id, resolution, *, evidence_ref=None, now: datetime | None = None
) -> BillingImportIssue:
    """Mbyll një çështje VETËM me veprim/evidencë reale (shih blloqun lart). Pa ndryshim të dhënash financiare; një herë; e audituar."""
    actor = money_common.admin(actor)
    why = money_common.reason(resolution)
    row = db.get(BillingImportIssue, money_common.uid(issue_id, "issue id"))
    if row is None:
        raise NotFound("import issue not found")
    if row.resolved_at is not None:
        raise Conflict("issue is already resolved")
    cat = categorize(row.reason)
    if _superseded(db, row):
        kind, ref = "superseded", None
    elif evidence_ref is not None:
        if not _waivable(row, cat):
            raise Conflict(
                f"a {row.classification}/{cat} issue cannot be waived: fix it at the source and re-export ({ISSUE_POLICY.get(cat, ISSUE_POLICY['other'])['allowed']})"
            )
        ref = evidence_ref.strip() if isinstance(evidence_ref, str) else ""
        if (
            not (WAIVER_REF_MIN <= len(ref) <= WAIVER_REF_MAX)
            or ";" in ref
            or any(ord(c) < 32 for c in ref)
        ):
            raise Invalid(
                f"evidence_ref must be {WAIVER_REF_MIN}..{WAIVER_REF_MAX} printable characters without ';' (ticket/document reference)"
            )
        kind = "operator_waiver"
    else:
        raise Conflict(
            "no evidence of a real resolution: re-export after fixing the source (a later batch must import or re-flag this object), "
            "or — only for requires_manual_review rows — pass evidence_ref for a documented waiver"
        )
    text = f"kind={kind}; " + (f"ref={ref}; " if ref else "") + why
    if len(text) > 500:
        raise Invalid("reason too long for the evidence record")
    now = billing.utc(now or utcnow())
    row.resolved_at, row.resolved_by_id, row.resolution = now, actor.id, text
    db.flush()
    audit.record(db, actor, "billing.import_issue_resolve", "billing_import_issue", row.id,
                 {"source_table": row.source_table, "source_id": row.source_id, "classification": row.classification, "category": cat, "kind": kind, "evidence_ref": ref, "resolution": why}, now=now)  # fmt: skip
    return row


def waivers(db: Session) -> list[BillingImportIssue]:
    return [r for r in db.scalars(select(BillingImportIssue).where(BillingImportIssue.resolved_at.is_not(None))) if (r.resolution or "").startswith("kind=operator_waiver")]  # fmt: skip


def unresolved_issues(db: Session) -> list[BillingImportIssue]:
    return list(
        db.scalars(
            select(BillingImportIssue)
            .where(BillingImportIssue.resolved_at.is_(None))
            .order_by(BillingImportIssue.created_at)
        )
    )


def _record_payment(db, inv: dict, pay: Payment, batch, now) -> None:
    """Origjina e pagesës së importuar: `payments` (id i pagesës online legacy) ose `wallet_settlements` (id i faturës; dëshmi e debitit wallet, pa rilozje)."""
    st = inv["settlement"]
    table, sid = (
        ("payments", str(st["payment_source_id"]))
        if st["kind"] == "online"
        else ("wallet_settlements", str(inv["source_id"]))
    )
    d = Decision(table, sid, "importable", core_hash=h(st), state_hash=h(st))
    _record_item(db, d, table, "payment", pay.id, batch, now, {"settlement_kind": st["kind"]})
