"""Rakordim i regjistrimeve të miratuara pa provisioning të përfunduar (M8-c).

    python -m apps.central.tools.reconcile_registrations                  # dry-run (default)
    python -m apps.central.tools.reconcile_registrations --apply          # provision të gjitha
    python -m apps.central.tools.reconcile_registrations --apply --request-id <uuid>

Liston `status=approved ∧ provisioning_status ∈ {pending, failed}`. Dry-run NUK ndryshon asgjë.
`--apply` bën nga NJË përpjekje për kërkesë (pa retry të pafund, pa sfond); dështimi regjistrohet
si `failed` + kod i qëndrueshëm. Kodet e daljes: 0 ok · 1 të paktën një dështim · 2 gabim hyrjeje.
"""

import argparse
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError
from apps.central.models.registration import APPROVED, FAILED, PENDING, RegistrationRequest
from apps.central.services import provisioning


def candidates(db, request_id=None) -> list[RegistrationRequest]:
    q = select(RegistrationRequest).where(
        RegistrationRequest.status == APPROVED,
        RegistrationRequest.provisioning_status.in_((PENDING, FAILED)),
    )
    if request_id is not None:
        q = q.where(RegistrationRequest.id == request_id)
    return list(db.scalars(q.order_by(RegistrationRequest.created_at, RegistrationRequest.id)))


def reconcile(factory: sessionmaker, *, apply: bool = False, request_id=None) -> list[dict]:
    with factory() as db:
        rows = [
            (r.id, r.provisioning_status, r.provisioning_error_code)
            for r in candidates(db, request_id)
        ]
    out = []
    for rid, st, code in rows:
        item = {"request_id": str(rid), "provisioning_status": st, "last_error": code}
        if apply:
            try:
                res = provisioning.run(factory, rid)
                item.update(result=res.status, error_code=res.error_code, attempts=res.attempts)
            except CentralError as e:
                item.update(result="skipped", error_code=type(e).__name__)
        out.append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile approved registrations (dry-run by default)."
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--request-id")
    a = ap.parse_args(argv)
    rid = None
    if a.request_id:
        try:
            rid = uuid.UUID(a.request_id)
        except ValueError:
            print("invalid --request-id", file=sys.stderr)
            return 2
    factory = sessionmaker(bind=make_engine(settings.database_url), expire_on_commit=False)
    items = reconcile(factory, apply=a.apply, request_id=rid)
    for i in items:
        print(" ".join(f"{k}={v}" for k, v in i.items()))
    print(f"{'applied' if a.apply else 'dry-run'}: {len(items)} candidate(s)")
    return 1 if any(i.get("result") == "failed" for i in items) else 0


if __name__ == "__main__":
    sys.exit(main())
