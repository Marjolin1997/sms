"""M11-J/G-07: retention i `sms_dlr_receipts`. Dry-run parazgjedhje; fshin vetëm me `--apply`.

    python -m scripts.dlr_receipt_retention [--days N] [--batch 1000] [--max-batches 1000] [--apply] [--json]

  · Pragu: `--days` ose `SMS_DLR_RECEIPT_RETENTION_DAYS` (parazgjedhje 90, min 7). Fshihen vetëm rreshtat me `received_at` më të vjetër se pragu;
    asnjë rresht më i ri se pragu nuk preket. Rreshtat historikë me trupin e papërpunuar (mund të përmbajnë numra telefoni) hiqen nga ky retention.
  · Fshirja bëhet në grupe (`--batch`), me commit për grup, nën një kyç advisory PostgreSQL (një ekzekutim aktiv për DB; SQLite: pa kyç).
  · Dalja përmban VETËM numërues (sipas rezultatit); kurrë id telefoni, trupa apo ID provider-i. Audit sistemi për `--apply` (numërues).
NUK lidhet me worker-in dhe NUK planifikohet automatikisht. Kodi: 0 ok · 1 kyçi mbahet nga një tjetër · 2 gabim."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.sending import DlrReceipt
from app.services import audit

ACTOR = "system:retention"
LOCK_KEY = 0x444C5252  # "DLRR"


def plan(db, cutoff: datetime) -> dict:
    rows = db.execute(
        select(DlrReceipt.outcome, func.count(), func.sum(DlrReceipt.seen_count))
        .where(DlrReceipt.received_at < cutoff)
        .group_by(DlrReceipt.outcome)
    ).all()
    keep = db.scalar(
        select(func.count()).select_from(DlrReceipt).where(DlrReceipt.received_at >= cutoff)
    )
    return {
        "cutoff_utc": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "would_delete": {o: int(n) for o, n, _ in rows},
        "would_delete_total": sum(int(n) for _, n, _ in rows),
        "callbacks_covered": sum(int(s or 0) for _, _, s in rows),
        "kept": int(keep or 0),
    }


def delete_batches(db, cutoff: datetime, batch: int, max_batches: int) -> int:
    total = 0
    for _ in range(max_batches):
        ids = list(
            db.scalars(
                select(DlrReceipt.id)
                .where(DlrReceipt.received_at < cutoff)
                .order_by(DlrReceipt.id)
                .limit(batch)
            )
        )
        if not ids:
            break
        res = db.execute(delete(DlrReceipt).where(DlrReceipt.id.in_(ids)))
        db.commit()
        total += res.rowcount or 0
    return total


def run(
    db, *, days: int, batch: int, max_batches: int, apply: bool, now: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    out = plan(db, cutoff)
    out.update(mode="apply" if apply else "dry-run", retention_days=days)
    if apply:
        out["deleted"] = delete_batches(db, cutoff, batch, max_batches)
        audit.system_event(db, ACTOR, "dlr_receipt.retention", "table", "sms_dlr_receipts",
                           {"deleted": out["deleted"], "cutoff_utc": out["cutoff_utc"], "retention_days": days})  # fmt: skip
        db.commit()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Retention of sms_dlr_receipts (dry-run unless --apply)."
    )
    ap.add_argument("--days", type=int, default=settings.dlr_receipt_retention_days)
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--max-batches", type=int, default=1000)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.days < 7 or a.batch < 1 or a.max_batches < 1:
        print("ERROR: --days >= 7, --batch >= 1, --max-batches >= 1", file=sys.stderr)
        return 2
    lock = None
    try:
        if a.apply and engine.dialect.name == "postgresql":
            lock = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            if not lock.execute(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
                print("another retention run holds the lock", file=sys.stderr)
                return 1
        with SessionLocal() as db:
            res = run(db, days=a.days, batch=a.batch, max_batches=a.max_batches, apply=a.apply)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {type(e).__name__}", file=sys.stderr)
        return 2
    finally:
        if (
            lock is not None
        ):  # kyçi advisory është i nivelit të sesionit: lirohet shprehimisht (lidhja kthehet te pool-i, nuk mbyllet)
            lock.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            lock.close()
    if a.json:
        print(json.dumps(res, sort_keys=True))
    else:
        print(f"{res['mode']}: cutoff {res['cutoff_utc']} would_delete={res['would_delete_total']} {res['would_delete']} kept={res['kept']}"
              + (f" deleted={res['deleted']}" if "deleted" in res else ""))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
