"""M9-f: retention i kufizuar. VETËM `usage_reports` (evidencë operacionale e rakordimit), kurrë histori financiare.

NUK preket kurrë: ledger tregtar, grant-et, pagesat, `money_events`, audit financiar, versionet/rregullat e çmimeve.
Politika (konfigurimi `CENTRAL_USAGE_REPORT_*`; `retention_days = 0` ⇒ pa fshirje):
  · raporti AKTUAL per (enterprise, product, currency) dhe `keep_last` të fundit ruhen gjithmonë;
  · moshë ≤ `full_days`: ruhen të gjitha; mes `full_days` dhe `retention_days`: një raport per ditë UTC (i fundit);
  · më i vjetër se `retention_days`: fshihet (përveç dy rasteve të para).
Fshirja bëhet vetëm nga ky modul, në një transaksion me `central.retention_delete = on` (trigger PG), dhe auditohet
(no-op ⇒ pa audit). Dry-run është parazgjedhja e mjetit."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.timeutil import utcnow
from apps.central.models.usage import UsageReport
from apps.central.services import audit, money_reconciliation

LABEL = "system:retention"


@dataclass(slots=True)
class Plan:
    delete_ids: list[uuid.UUID] = field(default_factory=list)
    kept: int = 0
    by_key: dict[str, int] = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "to_delete": len(self.delete_ids),
            "kept": self.kept,
            "by_key": self.by_key,
            "params": self.params,
        }


def plan(db: Session, *, now: datetime | None = None, retention_days: int | None = None,
         full_days: int | None = None, keep_last: int | None = None) -> Plan:  # fmt: skip
    now = now or utcnow()
    rd = settings.usage_report_retention_days if retention_days is None else retention_days
    fd = settings.usage_report_full_days if full_days is None else full_days
    kl = settings.usage_report_keep_last if keep_last is None else keep_last
    out = Plan(params={"retention_days": rd, "full_days": fd, "keep_last": kl})
    rows = db.execute(select(UsageReport.report_id, UsageReport.enterprise_id, UsageReport.product_id,
                             UsageReport.currency, UsageReport.report_seq, UsageReport.received_at)
                      .order_by(UsageReport.enterprise_id, UsageReport.product_id, UsageReport.currency,
                                UsageReport.report_seq.desc())).all()  # fmt: skip
    if rd == 0:
        out.kept = len(rows)
        return out
    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r.enterprise_id, r.product_id, r.currency), []).append(r)
    for key, items in groups.items():  # `items` renditur me seq zbritës: items[0] = aktuali
        seen_days: set = set()
        for i, r in enumerate(items):
            age = money_reconciliation.as_utc(now) - money_reconciliation.as_utc(r.received_at)
            day = money_reconciliation.as_utc(r.received_at).date()
            if i < kl:  # përfshin aktualin (i = 0)
                out.kept += 1
                seen_days.add(day)
            elif age <= timedelta(days=fd):
                out.kept += 1
                seen_days.add(day)
            elif age <= timedelta(days=rd) and day not in seen_days:
                seen_days.add(day)
                out.kept += 1
            else:
                out.delete_ids.append(r.report_id)
                label = f"{key[0]}/{key[1]}/{key[2]}"
                out.by_key[label] = out.by_key.get(label, 0) + 1
    return out


def apply(db: Session, p: Plan, *, now: datetime | None = None, chunk: int = 500) -> int:
    """Fshin sipas planit (në transaksionin e thirrësit; pa commit). Audit vetëm kur fshin diçka."""
    if not p.delete_ids:
        return 0
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('central.retention_delete', 'on', true)"))
    n = 0
    for i in range(0, len(p.delete_ids), chunk):
        res = db.execute(
            delete(UsageReport).where(UsageReport.report_id.in_(p.delete_ids[i : i + chunk]))
        )
        n += res.rowcount or 0
    audit.record_system(db, label=LABEL, action="usage_report.retention", resource_type="usage_report",
                        resource_id="retention", detail={"deleted": n, "kept": p.kept, "by_key": p.by_key,
                                                         "params": p.params}, now=now)  # fmt: skip
    return n
