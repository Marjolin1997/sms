"""M9-g2: marrja e raporteve kumulative të email-eve të faturueshme (`cp.billing.usage.v1`) + pyetjet që i duhen faturimit.

Ingest (idempotent, append-only, pa commit — tx i thirrësit):
- `report_id` i njëjtë + payload i njëjtë ⇒ `duplicate`; payload tjetër ⇒ Conflict. `(enterprise, product, report_seq)` i zënë nga raport tjetër ⇒ Conflict.
- Monotonia kundrejt fqinjëve sipas `report_seq`: `cumulative_billable_count`, `watermark` dhe `generated_at` s'bien kurrë me seq-un
  (rikthim/restore/falsifikim ⇒ Conflict, raporti NUK ruhet). Numërues që bie do të sillte faturim negativ ose të dyfishtë.
- `generated_at` nuk mund të jetë në të ardhmen (tolerancë e kufizuar): raport i së ardhmes do të "mbulonte" periudha që s'kanë mbaruar.
- Produkti duhet të jetë i kanalit `email`; enterprise/produkt duhet të ekzistojnë. Asnjë efekt financiar këtu.

Pyetjet e faturimit (vetëm lexim): `cutoff_report` = raporti i parë (sipas seq) me `generated_at >= period_end`;
`baseline_report` = raporti që mbyll periudhën e mëparshme, ose më i fundit me `generated_at <= period_start`."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.billing_usage import BillingUsageReport
from apps.central.models.enterprise import Enterprise
from apps.central.models.product import Product
from packages.contracts.control_plane.billing import usage_v1 as bv

MAX_BODY_BYTES = 4_096
FUTURE_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class Ingested:
    row: BillingUsageReport
    created: bool
    latest: bool


def parse(raw: object) -> bv.BillingUsageReportV1:
    try:
        return bv.BillingUsageReportV1.parse(raw)
    except bv.ContractError as e:
        raise Invalid(f"invalid billing usage report: {e}") from e


def _key(r: bv.BillingUsageReportV1):
    return uuid.UUID(r.enterprise_id), uuid.UUID(r.product_id)


def latest(db: Session, enterprise_id, product_id) -> BillingUsageReport | None:
    return db.scalar(
        select(BillingUsageReport)
        .where(
            BillingUsageReport.enterprise_id == enterprise_id,
            BillingUsageReport.product_id == product_id,
        )
        .order_by(BillingUsageReport.report_seq.desc())
        .limit(1)
    )


def cutoff_report(
    db: Session, enterprise_id, product_id, period_end: datetime
) -> BillingUsageReport | None:
    """Raporti i parë i pranuar (sipas seq) që pretendon gjendjen e paktën deri në `period_end`."""
    return db.scalar(
        select(BillingUsageReport)
        .where(BillingUsageReport.enterprise_id == enterprise_id, BillingUsageReport.product_id == product_id,
               BillingUsageReport.generated_at >= period_end)
        .order_by(BillingUsageReport.report_seq)
        .limit(1)
    )  # fmt: skip


def baseline_before(
    db: Session, enterprise_id, product_id, at: datetime
) -> BillingUsageReport | None:
    """Raporti më i fundit me `generated_at <= at` (baseline kur s'ka periudhë të mëparshme me raport)."""
    return db.scalar(
        select(BillingUsageReport)
        .where(BillingUsageReport.enterprise_id == enterprise_id, BillingUsageReport.product_id == product_id,
               BillingUsageReport.generated_at <= at)
        .order_by(BillingUsageReport.report_seq.desc())
        .limit(1)
    )  # fmt: skip


def _lock_key(db: Session, ent: uuid.UUID, prod: uuid.UUID) -> None:
    """Serializon ingest-in për (enterprise, product): kontrollet e fqinjëve janë të sakta edhe me kërkesa paralele (PostgreSQL)."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"billing-usage:{ent}:{prod}"},
        )


def _check_monotone(db: Session, r: bv.BillingUsageReportV1) -> None:
    ent, prod = _key(r)
    base = select(BillingUsageReport).where(
        BillingUsageReport.enterprise_id == ent, BillingUsageReport.product_id == prod
    )
    lower = db.scalar(base.where(BillingUsageReport.report_seq < r.report_seq)
                      .order_by(BillingUsageReport.report_seq.desc()).limit(1))  # fmt: skip
    higher = db.scalar(base.where(BillingUsageReport.report_seq > r.report_seq)
                       .order_by(BillingUsageReport.report_seq).limit(1))  # fmt: skip
    gen = r.generated_at
    if lower is not None and (
        r.count < lower.cumulative_billable_count
        or r.watermark < lower.watermark
        or gen < _aware(lower.generated_at)
    ):
        raise Conflict(
            "usage report regresses against an earlier report_seq (restore or forged report?)"
        )
    if higher is not None and (
        r.count > higher.cumulative_billable_count
        or r.watermark > higher.watermark
        or gen > _aware(higher.generated_at)
    ):
        raise Conflict("usage report is ahead of a later report_seq (restore or forged report?)")


def _aware(dt: datetime) -> datetime:
    from apps.central.services.billing import utc

    return utc(dt)


def ingest(
    db: Session, report: bv.BillingUsageReportV1, *, now: datetime | None = None
) -> Ingested:
    now = _aware(now or utcnow())
    ent, prod = _key(report)
    rid = uuid.UUID(report.report_id)
    phash = report.payload_hash()
    prior = db.get(BillingUsageReport, rid)
    if prior is not None:
        if prior.payload_hash != phash:
            raise Conflict("report_id was already used with a different payload")
        return Ingested(prior, False, _is_latest(db, prior))
    if db.get(Enterprise, ent) is None:
        raise Invalid("unknown enterprise")
    product = db.get(Product, prod)
    if product is None:
        raise Invalid("unknown product")
    if product.channel != "email":
        raise Invalid("billing usage reports are only accepted for email products")
    if report.generated_at > now + FUTURE_TOLERANCE:
        raise Invalid("generated_at is in the future")
    _lock_key(db, ent, prod)
    prior = db.get(
        BillingUsageReport, rid
    )  # rilexim pas kyçjes (një kërkesë paralele mund ta ketë ruajtur)
    if prior is not None:
        if prior.payload_hash != phash:
            raise Conflict("report_id was already used with a different payload")
        return Ingested(prior, False, _is_latest(db, prior))
    _check_monotone(db, report)
    row = BillingUsageReport(
        report_id=rid, enterprise_id=ent, product_id=prod, report_seq=report.report_seq, watermark=report.watermark,
        cumulative_billable_count=report.count, generated_at=report.generated_at, received_at=now,
        payload=report.to_dict(), payload_hash=phash, content_hash=report.content_hash(), schema_version=bv.SCHEMA,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        db.expire_all()
        again = db.get(BillingUsageReport, rid)
        if again is not None:
            if again.payload_hash != phash:
                raise Conflict("report_id was already used with a different payload") from None
            return Ingested(again, False, _is_latest(db, again))
        raise Conflict("report_seq is already taken by a different report") from None
    return Ingested(row, True, _is_latest(db, row))


def _is_latest(db: Session, row: BillingUsageReport) -> bool:
    top = latest(db, row.enterprise_id, row.product_id)
    return top is not None and top.report_id == row.report_id
