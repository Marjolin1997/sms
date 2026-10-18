"""M9-d: marrja e raporteve kumulative të përdorimit financiar (immutable, idempotente). Pa commit (tx i thirrësit).

- `report_id` i njëjtë + payload i njëjtë ⇒ no-op (`duplicate`); payload tjetër ⇒ Conflict.
- (enterprise, product, currency, report_seq) i njëjtë me `report_id` tjetër ⇒ Conflict.
- Raport më i vjetër që vonohet: ruhet në histori, KURRË nuk bëhet aktual (aktual = report_seq më i madh).
- Monotonia e watermark-ut: `ledger_max_id` s'bie kurrë me `report_seq` (kundrejt fqinjëve më të vegjël/më të mëdhenj).
- Vlefshmëria: skema/Decimal/totale ≥ 0 në kontratë; enterprise dhe produkt duhet të ekzistojnë në Central.
Nuk ka asnjë efekt financiar: raportet vetëm krahasohen (shih `money_reconciliation`)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.product import Product
from apps.central.models.usage import UsageReport
from packages.contracts.control_plane.money import usage_v1 as uv

MAX_BODY_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class Ingested:
    row: UsageReport
    created: bool
    latest: bool  # është raporti aktual (seq më i madh) pas këtij hyrjeje


def parse(raw: object) -> uv.UsageReportV1:
    try:
        return uv.UsageReportV1.parse(raw)
    except uv.ContractError as e:
        raise Invalid(f"invalid usage report: {e}") from e


def _key(r: uv.UsageReportV1):
    return (uuid.UUID(r.enterprise_id), uuid.UUID(r.product_id), r.currency)


def latest(db: Session, enterprise_id, product_id, currency) -> UsageReport | None:
    return db.scalar(
        select(UsageReport)
        .where(UsageReport.enterprise_id == enterprise_id, UsageReport.product_id == product_id,
               UsageReport.currency == currency)
        .order_by(UsageReport.report_seq.desc())
        .limit(1)
    )  # fmt: skip


def latest_per_key(db: Session, enterprise_id=None) -> list[UsageReport]:
    """Raporti aktual per (enterprise, product, currency); pa gjendje të ruajtur."""
    q = select(UsageReport).order_by(UsageReport.enterprise_id, UsageReport.product_id,
                                     UsageReport.currency, UsageReport.report_seq.desc())  # fmt: skip
    if enterprise_id is not None:
        q = q.where(UsageReport.enterprise_id == enterprise_id)
    out, seen = [], set()
    for r in db.scalars(q):
        k = (r.enterprise_id, r.product_id, r.currency)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def history(
    db: Session, enterprise_id, product_id, currency, limit: int = 100
) -> list[UsageReport]:
    return list(
        db.scalars(
            select(UsageReport)
            .where(UsageReport.enterprise_id == enterprise_id, UsageReport.product_id == product_id,
                   UsageReport.currency == currency)
            .order_by(UsageReport.report_seq.desc())
            .limit(max(1, min(limit, 500)))
        )
    )  # fmt: skip


def _check_watermark(db: Session, r: uv.UsageReportV1) -> None:
    ent, prod, cur = _key(r)
    base = select(UsageReport.ledger_max_id).where(
        UsageReport.enterprise_id == ent,
        UsageReport.product_id == prod,
        UsageReport.currency == cur,
    )
    lower = db.scalar(base.where(UsageReport.report_seq < r.report_seq)
                      .order_by(UsageReport.report_seq.desc()).limit(1))  # fmt: skip
    higher = db.scalar(base.where(UsageReport.report_seq > r.report_seq)
                       .order_by(UsageReport.report_seq).limit(1))  # fmt: skip
    wm = r.doc["ledger_max_id"]
    if (lower is not None and wm < lower) or (higher is not None and wm > higher):
        raise Conflict(
            "ledger watermark is not monotone with report_seq (restore or forged report?)"
        )


def ingest(db: Session, report: uv.UsageReportV1, *, now: datetime | None = None) -> Ingested:
    ent, prod, cur = _key(report)
    rid = uuid.UUID(report.report_id)
    phash = report.payload_hash()
    prior = db.get(UsageReport, rid)
    if prior is not None:
        if prior.payload_hash != phash:
            raise Conflict("report_id was already used with a different payload")
        return Ingested(prior, False, _is_latest(db, prior))
    if db.get(Enterprise, ent) is None:
        raise Invalid("unknown enterprise")
    if db.get(Product, prod) is None:
        raise Invalid("unknown product")
    _check_watermark(db, report)
    d = report.doc
    row = UsageReport(
        report_id=rid, enterprise_id=ent, product_id=prod, currency=cur, report_seq=report.report_seq,
        authority_mode=d["authority_mode"], generated_at=report.generated_at, received_at=now or utcnow(),
        ledger_max_id=d["ledger_max_id"], money_cursor_seq=d["cursor"]["last_seq"],
        money_cursor_epoch=uuid.UUID(d["cursor"]["epoch"]) if d["cursor"]["epoch"] else None,
        available=Decimal(d["wallet"]["available"]), held=Decimal(d["wallet"]["held"]),
        gross=Decimal(d["wallet"]["gross"]), payload=d, payload_hash=phash, schema_version=uv.SCHEMA,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        db.expire_all()
        again = db.get(UsageReport, rid)  # garë: i njëjti report_id nga një kërkesë tjetër
        if again is not None:
            if again.payload_hash != phash:
                raise Conflict("report_id was already used with a different payload") from None
            return Ingested(again, False, _is_latest(db, again))
        raise Conflict("report_seq is already taken by a different report") from None
    return Ingested(row, True, _is_latest(db, row))


def _is_latest(db: Session, row: UsageReport) -> bool:
    top = latest(db, row.enterprise_id, row.product_id, row.currency)
    return top is not None and top.report_id == row.report_id
