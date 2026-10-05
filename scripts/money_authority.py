"""M9-c: veprime operatori për cutover-in e parave (Enterprise vetëm; Central s'mutohet kurrë nga këtu).

    python -m scripts.money_authority baseline-create --wallet-id N --by <operator>   # kërkon authority=shadow
    python -m scripts.money_authority baseline-show   [--wallet-id N]
    python -m scripts.money_authority cursor-show
    python -m scripts.money_authority reset-cursor --epoch <uuid> --generation N --ack-replay

`baseline-create` regjistron `gross_at_cutover` (available+held) të pandryshueshëm dhe shtyp `baseline_ref`,
që stafi i Central e jep te grant-i `purpose=bootstrap`. Nuk krijon para. `reset-cursor` është për epokë të
re të Central (restore): riprodhim nga 0, idempotent."""

import argparse
import json
import sys
import uuid

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.money_authority import MoneyBaseline
from app.services import money_authority as ma
from app.services import money_sync as ms


def _show(b: MoneyBaseline) -> dict:
    return {
        "baseline_ref": b.baseline_ref, "wallet_id": b.wallet_id, "enterprise_id": str(b.enterprise_id),
        "currency": b.currency, "product_id": str(b.product_id), "available": str(b.available_at_cutover),
        "held": str(b.held_at_cutover), "gross": str(b.gross_at_cutover),
        "ledger_max_id": b.ledger_max_id, "created_at": b.created_at.isoformat(),
        "status": b.status, "hash_valid": ma.baseline_valid(b),
    }  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("baseline-create")
    c.add_argument("--wallet-id", type=int, required=True)
    c.add_argument("--by", required=True)
    s = sub.add_parser("baseline-show")
    s.add_argument("--wallet-id", type=int)
    sub.add_parser("cursor-show")
    r = sub.add_parser("reset-cursor")
    r.add_argument("--epoch", required=True)
    r.add_argument("--generation", type=int, required=True)
    r.add_argument("--ack-replay", action="store_true")
    a = ap.parse_args(argv)
    with SessionLocal() as db:
        try:
            if a.cmd == "baseline-create":
                b = ma.create_baseline(db, a.wallet_id, a.by)
                db.commit()
                print(json.dumps(_show(b), indent=1))  # noqa: T201
            elif a.cmd == "baseline-show":
                q = select(MoneyBaseline).order_by(MoneyBaseline.id)
                if a.wallet_id:
                    q = q.where(MoneyBaseline.wallet_id == a.wallet_id)
                print(json.dumps([_show(b) for b in db.scalars(q)], indent=1))  # noqa: T201
            elif a.cmd == "cursor-show":
                cur = ms.get_cursor(db)
                print(json.dumps({  # noqa: T201
                    "epoch": str(cur.epoch), "generation": cur.authorization_generation,
                    "last_seq": cur.last_seq, "last_success_at": str(cur.last_success_at),
                    "last_error": cur.last_error}, indent=1))  # fmt: skip
                db.rollback()
            else:
                if not a.ack_replay:
                    print("refused: --ack-replay is required", file=sys.stderr)  # noqa: T201
                    return 2
                ms.reset_epoch(db, uuid.UUID(a.epoch), a.generation)
                db.commit()
                print("cursor reset; the consumer will replay from seq 0")  # noqa: T201
        except (ma.BaselineError, ValueError) as e:
            db.rollback()
            print(f"refused: {e}", file=sys.stderr)  # noqa: T201
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
