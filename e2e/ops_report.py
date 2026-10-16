"""E2E M11-D2: raportuesi Enterprise REAL (builder + ControlPlaneClient me Ed25519) drejt Central.

VETËM DB e përkohshme e zhvillimit. Mjedisi (SMS_DATABASE_URL, SMS_CP_*, SMS_ENV=development) vendoset nga thirrësi:

    python e2e/ops_report.py base     # gjendje Enterprise e shëndetshme (kursori me lag 5, 1 kërkesë në pritje, 120 krahasime shadow)
    python e2e/ops_report.py drift    # shton 6 krahasime kritike (central_missing) dhe raporton përsëri

Thërret `sender_ops_snapshot.report_once(force=True)` (rruga e prodhimit: ndërtim lokal, HTTP jashtë transaksionit) dhe printon vetëm statusin e Central."""

import sys
import uuid
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.sender_authority import (
    SenderAuthorityComparison,
    SenderBootstrapIssue,
    SenderBootstrapState,
)
from app.models.sender_sync import SenderSyncCursor
from app.services import control_plane_client as cc
from app.services import sender_ids as sid
from app.services import sender_ops_snapshot as ops


def add_comparisons(db, category, n, tag):
    for i in range(n):
        db.add(
            SenderAuthorityComparison(
                ref=f"{tag}{i}",
                country="AL",
                category=category,
                local_allowed=True,
                central_allowed=True,
                central_reason="x",
                identity_hash="0" * 16,
            )
        )


def seed_base(db):
    now = datetime.now(UTC)
    s = sid.request(db, "e2e-acme", "AL", "E2EPEND")
    cur = db.get(SenderSyncCursor, 1) or SenderSyncCursor(id=1)
    cur.epoch, cur.last_seq, cur.latest_central_seq, cur.last_success_at = (
        uuid.uuid4(),
        40,
        45,
        now - timedelta(seconds=20),
    )
    cur.failure_count, cur.gap_recoveries, cur.drift_repairs = 0, 0, 0
    db.add(cur)
    add_comparisons(db, "match_allowed", 120, "m")
    st = db.get(SenderBootstrapState, 1) or SenderBootstrapState(id=1)
    st.bootstrap_version, st.started_at, st.completed_at, st.source_revision = (
        1,
        now,
        now,
        "e2e-rev",
    )
    st.tenant_count, st.sender_count, st.unresolved_count, st.report_hash = 2, 10, 3, "ab" * 32
    db.add(st)
    db.add(
        SenderBootstrapIssue(
            enterprise_id=None, sender_id=s.id, category="identity_conflict", identity_hash="1" * 16
        )
    )
    db.commit()


def main(scenario: str) -> int:
    with SessionLocal() as db:
        if scenario == "base":
            seed_base(db)
        elif scenario == "drift":
            add_comparisons(db, "central_missing", 6, "d")
            db.commit()
        else:
            raise SystemExit("scenario must be base|drift")
    ops.clear_cache()  # alarmet rivlerësohen (proces i ri çdo herë, por në mënyrë të shprehur)
    client = cc.ControlPlaneClient(cc.config_from_settings(settings), scope=cc.SENDER_REPORT_SCOPE)
    try:
        out = ops.report_once(SessionLocal, client, force=True)
    finally:
        client.close()
    print(f"report: {out.kind} {out.status} {out.detail}")  # noqa: T201
    return 0 if out.kind == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
