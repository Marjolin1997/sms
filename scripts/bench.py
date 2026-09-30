"""Matje kapaciteti (vetëm për një bazë PostgreSQL të dedikuar `*_bench`; refuzon çdo tjetër).

    createdb sms_bench && SMS_DATABASE_URL=postgresql+psycopg://…/sms_bench alembic upgrade head
    SMS_DATABASE_URL=… SMS_PII_HMAC_KEY=bench SMS_SECRETS_KEY=<fernet> \\
        python -m scripts.bench --accounts 20 --messages 4000 --concurrency 32 --workers 2

Faza A (pranimi): API-ja real (uvicorn) merr POST /v1/messages me çelës Idempotency-Key unik;
    llogaritet req/s dhe latenca p50/p95/p99, për (1) një llogari të vetme ("hot wallet") dhe
    (2) mesazhet të shpërndara në N llogari.
Faza B (dërgimi): workers e vërtetë (`python -m app.worker`) shkarkojnë radhën me provider `fake`
    (pa rrjet); matet mesazhe/s deri sa radha bosh, pastaj kontrollohet integriteti i parave.
Provider-i i vërtetë (Twilio) e ngadalëson dërgimin; kjo mat pjesën tonë (DB, kyçje, ledger)."""

import argparse
import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select, text

from app.core.db import SessionLocal, engine
from app.models.sending import AccountPlan, Message, MessageStatus, Route
from app.services import apikeys, rates, sender_ids
from app.services import wallet as wallets

PAST = datetime(2020, 1, 1, tzinfo=UTC)


def guard() -> None:
    url = str(engine.url)
    if not url.startswith("postgresql") or not engine.url.database.endswith("_bench"):
        sys.exit(
            "refuzohet: SMS_DATABASE_URL duhet të jetë një bazë PostgreSQL me emër që mbaron me _bench"
        )


def setup(accounts: int) -> list[tuple[str, str]]:
    """→ [(owner, api_key)]. Çdo llogari: wallet 1 000 000 EUR, sender BENCH<i>, çmim 0.05, rrugë fake."""
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Message)):
            sys.exit("baza _bench ka tashmë mesazhe: krijoni një bazë të re")
        card = rates.create_card(db, "bench", "EUR")
        v = rates.new_draft(db, card.id)
        rates.set_rate(db, v.id, "355", "0.05")
        rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
        if not db.scalar(select(Route).where(Route.prefix == "355")):
            db.add(Route(prefix="355", country="AL", provider="fake"))
        out = []
        for i in range(accounts):
            owner = f"bench{i}"
            w = wallets.create_wallet(db, owner, "EUR")
            wallets.confirm_topup(
                db, wallets.create_topup(db, w.id, "1000000", wallets.TopupMethod.CASH).id
            )
            db.add(AccountPlan(owner_ref=owner, rate_card_id=card.id, rate_limit_per_min=10**9))
            s = sender_ids.request(db, owner, "AL", f"BENCH{i}")
            sender_ids.approve(db, s.id, "bench")
            _, key = apikeys.create_key(db, f"bench{i}", "client", owner, "bench")
            out.append((owner, key))
        db.commit()
    return out


async def accept_phase(base: str, keys: list[tuple[str, str]], total: int, conc: int, tag: str):
    lat: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(conc)
    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=60) as c:

        async def one(i: int):
            nonlocal errors
            owner, key = keys[i % len(keys)]
            body = {
                "owner_ref": owner,
                "to": f"+3556912{i % 100000:05d}",
                "sender": "BENCH" + owner.removeprefix("bench"),
                "text": f"bench {i}",
            }
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await c.post(
                        "/v1/messages",
                        json=body,
                        headers={"Authorization": f"Bearer {key}", "Idempotency-Key": f"{tag}-{i}"},
                    )
                    if r.status_code != 202:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                lat.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        wall = time.perf_counter() - t0
    lat.sort()
    q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] * 1000  # noqa: E731
    return {
        "n": total,
        "errors": errors,
        "rps": total / wall,
        "p50": q(0.5),
        "p95": q(0.95),
        "p99": q(0.99),
    }


def queue_depth() -> int:
    with SessionLocal() as db:
        return db.scalar(
            select(func.count()).select_from(Message).where(Message.status == MessageStatus.QUEUED)
        )


def drain_phase(workers: int) -> dict:
    start_depth = queue_depth()
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "app.worker"],
            env=os.environ | {"SMS_WORKER_HEARTBEAT": f"/tmp/bench-hb-{i}"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(workers)
    ]  # noqa: S603
    t0 = time.perf_counter()
    try:
        while queue_depth() > 0:
            time.sleep(0.5)
            if time.perf_counter() - t0 > 900:
                sys.exit("timeout: radha nuk u zbraz brenda 15 minutave")
        wall = time.perf_counter() - t0
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
    return {
        "messages": start_depth,
        "workers": workers,
        "seconds": wall,
        "per_second": start_depth / wall if wall else 0,
    }


def integrity() -> str:
    with SessionLocal() as db:
        bad = 0
        for w in db.scalars(select(wallets.Wallet)):
            if not wallets.verify_wallet(db, w.id):
                bad += 1
        stuck = db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.status.in_((MessageStatus.QUEUED, MessageStatus.SENDING)))
        )
        dup = db.execute(
            text(
                "select count(*) from (select owner_ref, idempotency_key from sms_messages group by 1,2 having count(*)>1) x"
            )
        ).scalar()
    return f"wallets inkonsistente={bad}, mesazhe të pa-përfunduara={stuck}, dublikate idempotence={dup}"


def show(name: str, r: dict) -> None:
    print(
        f"{name:<34} {r['rps']:>8.0f} req/s   p50 {r['p50']:>6.0f} ms   p95 {r['p95']:>6.0f} ms   p99 {r['p99']:>6.0f} ms   gabime {r['errors']}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", type=int, default=20)
    ap.add_argument("--messages", type=int, default=4000)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument(
        "--base",
        default="http://127.0.0.1:8000",
        help="API-ja që po punon (uvicorn) mbi të njëjtën bazë",
    )
    a = ap.parse_args()
    guard()
    keys = setup(a.accounts)
    print(f"→ {a.accounts} llogari, {a.messages} mesazhe/skenar, konkurrencë {a.concurrency}")
    r1 = asyncio.run(accept_phase(a.base, keys[:1], a.messages, a.concurrency, "hot"))
    show("A1 pranim: një wallet (hot)", r1)
    r2 = asyncio.run(accept_phase(a.base, keys, a.messages, a.concurrency, "spread"))
    show(f"A2 pranim: {a.accounts} llogari", r2)
    print(f"→ radha: {queue_depth()} mesazhe; nis {a.workers} workers…")
    d = drain_phase(a.workers)
    print(
        f"B  dërgim (fake): {d['messages']} mesazhe / {d['seconds']:.1f}s = {d['per_second']:.0f} mesazhe/s me {d['workers']} worker(s)"
    )
    print("Integriteti:", integrity())
    return 0


if __name__ == "__main__":
    sys.exit(main())
