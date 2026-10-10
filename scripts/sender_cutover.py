"""Mjetet e cutover-it të sender policy (M10-S5). Asnjë veprim i pakthyeshëm: ndërrimi i `SMS_SENDER_AUTHORITY` bëhet nga operatori.

    evidence  --actor A --code-revision REV [--central-readiness central.json]   # regjistron provën pre_cutover (refuzon nëse ka FAIL); printon ACK-un (evidence_hash)
    canary    --owner-ref O --country AL --sender S --to +355… [--send --key K]   # dry-run: vendimi i fasadës; --send: submit normal (pa përjashtim test)
    complete  --actor A --ref-hash H --canary-ref PUBLIC_ID --code-revision REV   # prova post_cutover (vetëm nën central)
    rollback  --to shadow|local [--accept-divergence --actor A --reason R] [--reconcile-local --actor A]
    status                                                                          # ACK, bootstrap, prova të fundit

Kodi: 0 sukses · 1 refuzuar/gatishmëri me FAIL · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.sender_authority import SenderCutoverEvidence
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_cutover as co


def _out(obj) -> None:
    print(json.dumps(obj, sort_keys=True, default=str))  # noqa: T201


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evidence")
    e.add_argument("--actor", required=True)
    e.add_argument("--code-revision", default="unknown")
    e.add_argument("--central-readiness")
    e.add_argument("--min-samples", type=int)
    e.add_argument("--window-hours", type=int)
    c = sub.add_parser("canary")
    c.add_argument("--owner-ref", required=True)
    c.add_argument("--country", required=True)
    c.add_argument("--sender", required=True)
    c.add_argument("--to", required=True)
    c.add_argument("--send", action="store_true")
    c.add_argument("--key")
    c.add_argument("--text", default="canary")
    k = sub.add_parser("complete")
    k.add_argument("--actor", required=True)
    k.add_argument("--ref-hash", required=True)
    k.add_argument("--canary-ref", required=True)
    k.add_argument("--code-revision", default="unknown")
    r = sub.add_parser("rollback")
    r.add_argument("--to", required=True, choices=("shadow", "local"))
    r.add_argument("--accept-divergence", action="store_true")
    r.add_argument("--reconcile-local", action="store_true")
    r.add_argument("--actor")
    r.add_argument("--reason")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    try:
        with (factory or SessionLocal)() as db:
            return _run(db, a)
    except co.EvidenceRefused as ex:
        print(f"refused: {ex}", file=sys.stderr)  # noqa: T201
        return 1
    except Exception as ex:  # noqa: BLE001
        print(f"internal error: {type(ex).__name__}", file=sys.stderr)  # noqa: T201
        return 2


def _run(db, a) -> int:
    if a.cmd == "evidence":
        central = (
            json.load(open(a.central_readiness, encoding="utf-8")) if a.central_readiness else None
        )  # noqa: SIM115
        payload = co.build_evidence(
            db,
            kind="pre_cutover",
            actor=a.actor,
            code_revision=a.code_revision,
            central_readiness=central,
            min_samples=a.min_samples,
            window_hours=a.window_hours,
        )
        row, created = co.record_evidence(db, payload)
        db.commit()
        _out(
            {
                "evidence_hash": row.evidence_hash,
                "created": created,
                "readiness": payload["readiness"]["status"],
                "ack_env": f"SMS_SENDER_AUTHORITY_ACK={row.evidence_hash}",
            }
        )
        return 0
    if a.cmd == "canary":
        d = sau.check_outbound_authority(db, a.owner_ref, a.country.upper(), a.sender)
        res = {
            "mode": sau.mode(),
            "allowed": d.allowed,
            "source": d.source,
            "provenance": {k: str(v) for k, v in d.message_fields().items() if v is not None}
            if d.allowed
            else None,
        }
        if a.send:
            from app.services import messages as msgs

            m = msgs.submit(db, a.owner_ref, a.key or "canary", a.to, a.sender, text=a.text)
            db.commit()
            res.update(
                {
                    "sent": True,
                    "message": m.public_id,
                    "authority_source": m.sender_authority_source,
                }
            )
        _out(res)
        return 0 if d.allowed else 1
    if a.cmd == "complete":
        row, created = co.complete_cutover(
            db,
            actor=a.actor,
            ref_hash=a.ref_hash,
            canary_ref=a.canary_ref,
            code_revision=a.code_revision,
        )
        db.commit()
        _out({"evidence_hash": row.evidence_hash, "created": created})
        return 0
    if a.cmd == "rollback":
        items = ar.rollback_checks(db, a.to, accept_divergence=a.accept_divergence)
        status = ar.overall(items)
        if a.reconcile_local:
            if not a.actor:
                raise co.EvidenceRefused("--actor is required with --reconcile-local")
            n = co.reconcile_local_for_rollback(db, a.actor)
            db.commit()
            _out({"locally_revoked": n})
            items = ar.rollback_checks(db, a.to)
            status = ar.overall(items)
        if a.accept_divergence:
            if not (a.actor and a.reason):
                raise co.EvidenceRefused("--accept-divergence requires --actor and --reason")
            payload = co.build_evidence(
                db, kind="rollback_ack", actor=a.actor, code_revision="rollback"
            )
            payload["rollback"] = {
                "target": a.to,
                "reason": a.reason[:255],
                "divergence": ar.divergence(db),
            }
            co.record_evidence(db, payload)
            db.commit()
        _out(
            {
                "status": status,
                "checks": [asdict(x) for x in items],
                "next": f"set SMS_SENDER_AUTHORITY={a.to} and restart (manual)",
            }
        )
        return 1 if status == ar.FAIL else 0
    ok, why = ar.ack_status(db)
    last = [
        {"kind": x.kind, "hash": x.evidence_hash, "readiness": x.readiness_status}
        for x in db.scalars(
            select(SenderCutoverEvidence).order_by(SenderCutoverEvidence.id.desc()).limit(5)
        )
    ]
    _out(
        {
            "mode": settings.sender_authority,
            "ack_valid": ok,
            "ack_reason": why,
            "recent_evidence": last,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
