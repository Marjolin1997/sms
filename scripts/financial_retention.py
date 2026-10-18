"""M9-f: retention i kufizuar për të dhëna OPERACIONALE të Enterprise. Dry-run parazgjedhje; `--apply` fshin dhe auditon.

    python -m scripts.financial_retention [--apply] [--json] [--include-shadow]

  · `sms_pricing_comparisons`: rreshtat OK më të vjetër se `SMS_PRICING_COMPARISON_OK_DAYS` (0 = jo), mospërputhjet më të vjetra se
    `SMS_PRICING_COMPARISON_MISMATCH_DAYS`. Gjatë `SMS_PRICING_AUTHORITY=shadow` NUK fshihet (readiness e përdor dritaren), pa `--include-shadow`.
  · `sms_usage_reports` (outbox): vetëm `sent`/`superseded` më të vjetër se `SMS_USAGE_OUTBOX_RETENTION_DAYS` (0 = jo), pa
    `SMS_USAGE_OUTBOX_KEEP_LAST` të fundit per (enterprise, product, currency). Kurrë pending/sending/retry/failed.
NUK preket kurrë: ledger-i i wallet-it, hold-et, grant-et/baseline-t, mesazhet (foto e çmimit), faturat, snapshot-et/versionet e çmimeve.
Kodet: 0 ok · 2 gabim."""

import argparse
import json
import sys
from datetime import timedelta

from sqlalchemy import delete, select, text

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.timeutil import as_utc, utcnow
from app.models.money_usage import R_SENT, R_SUPERSEDED, UsageReport
from app.models.pricing import PricingComparison
from app.services import audit

ACTOR = "system:retention"


def plan(db, now, include_shadow: bool = False) -> dict:
    out: dict = {"comparisons": {"ok": [], "mismatch": [], "skipped": None}, "usage_outbox": []}
    s = settings
    if s.pricing_authority == "shadow" and not include_shadow:
        out["comparisons"]["skipped"] = (
            "SMS_PRICING_AUTHORITY=shadow: the shadow window is in use (use --include-shadow)"
        )
    else:
        if s.pricing_comparison_ok_days:
            cut = now - timedelta(days=s.pricing_comparison_ok_days)
            out["comparisons"]["ok"] = list(
                db.scalars(
                    select(PricingComparison.id).where(
                        PricingComparison.ok.is_(True), PricingComparison.created_at < cut
                    )
                )
            )
        cut = now - timedelta(days=s.pricing_comparison_mismatch_days)
        out["comparisons"]["mismatch"] = list(
            db.scalars(
                select(PricingComparison.id).where(
                    PricingComparison.ok.is_(False), PricingComparison.created_at < cut
                )
            )
        )
    if s.usage_outbox_retention_days:
        cut = now - timedelta(days=s.usage_outbox_retention_days)
        rows = db.execute(select(UsageReport.report_id, UsageReport.enterprise_id, UsageReport.product_id, UsageReport.currency, UsageReport.status, UsageReport.created_at)
                          .order_by(UsageReport.enterprise_id, UsageReport.product_id, UsageReport.currency, UsageReport.report_seq.desc())).all()  # fmt: skip
        seen: dict = {}
        for r in rows:
            k = (r.enterprise_id, r.product_id, r.currency)
            seen[k] = seen.get(k, 0) + 1
            if (
                seen[k] > s.usage_outbox_keep_last
                and r.status in (R_SENT, R_SUPERSEDED)
                and as_utc(r.created_at) < cut
            ):
                out["usage_outbox"].append(r.report_id)
    return out


def apply(db, p: dict, now) -> dict:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT set_config('sms.retention_delete', 'on', true)"))
    done = {"comparisons": 0, "usage_outbox": 0}
    ids = p["comparisons"]["ok"] + p["comparisons"]["mismatch"]
    for i in range(0, len(ids), 500):
        done["comparisons"] += (
            db.execute(
                delete(PricingComparison).where(PricingComparison.id.in_(ids[i : i + 500]))
            ).rowcount
            or 0
        )
    ids = p["usage_outbox"]
    for i in range(0, len(ids), 500):
        done["usage_outbox"] += (
            db.execute(
                delete(UsageReport).where(UsageReport.report_id.in_(ids[i : i + 500]))
            ).rowcount
            or 0
        )
    if any(done.values()):
        audit.system_event(
            db, ACTOR, "financial.retention", "retention", "*", {**done, "at": now.isoformat()}
        )
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Operational retention (dry-run by default).")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--include-shadow", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = utcnow()
        with SessionLocal() as db:
            p = plan(db, now, a.include_shadow)
            summary = {"comparisons_ok": len(p["comparisons"]["ok"]), "comparisons_mismatch": len(p["comparisons"]["mismatch"]),
                       "usage_outbox": len(p["usage_outbox"]), "skipped": p["comparisons"]["skipped"], "applied": a.apply}  # fmt: skip
            if a.apply:
                summary["deleted"] = apply(db, p, now)
                db.commit()
            else:
                db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"retention failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    print(
        json.dumps(summary, indent=1)
        if a.json
        else f"{'APPLIED' if a.apply else 'DRY-RUN'} {summary}"
    )  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
