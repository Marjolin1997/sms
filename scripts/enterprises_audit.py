"""Auditim vetëm-lexim i `owner_ref` legacy (M1a). Ekzekutoje PARA `alembic upgrade`:
    SMS_DATABASE_URL=… python -m scripts.enterprises_audit
Dalja 0 = asnjë anomali që ndalon migrimin; 1 = ka anomali (raporti tregon çfarë dhe ku).
Nuk shkruan asgjë. Me `--backfill` krijon Enterprise-t që mungojnë (idempotent; pas migrimit).
Me `--check` (pas 0019) kontrollon konsistencën owner_ref ↔ enterprise_id (dalja 1 nëse ka jo-përputhje;
me `--strict` edhe nëse ka rreshta pa enterprise_id)."""

import sys

from app.core.db import SessionLocal
from app.services import enterprises


def main() -> int:
    if "--check" in sys.argv:
        with SessionLocal() as db:
            rep = enterprises.check_consistency(db)
        print(rep.report())
        return 0 if (rep.complete if "--strict" in sys.argv else rep.ok) else 1
    with SessionLocal() as db:
        audit = enterprises.audit_owner_refs(db)
        print(audit.report())
        if "--backfill" in sys.argv and audit.ok:
            res = enterprises.backfill_missing(db)
            db.commit()
            print(f"backfill: krijuar {res.created}, ekzistonin {res.already_present}")
        return 0 if audit.ok else 1


if __name__ == "__main__":
    sys.exit(main())
