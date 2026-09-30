"""Sonda in-process për koston e dual-write (M1b), vetëm mbi bazë `*_bench`:
1. SQL për request tipik (POST /v1/messages) dhe për një cikël workeri: ON vs OFF
2. a hyn `before_flush` në rrugën e workerit, sa herë, a bën SELECT, autoflush
3. mikro-matje e shkrimit (krijo objekt tenant-owned + commit): resolver cold/warm/OFF, tenant i ri

  SMS_DATABASE_URL=…/probe_bench … python -m scripts.bench_probe"""

import statistics
import sys
import time
from collections import Counter

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from scripts.bench import guard, setup


class Counters:
    def __init__(self):
        self.sql = Counter()
        self.total = 0
        self.ent_selects = 0
        self.ent_inserts = 0
        self.before_flush = 0
        self.flush = 0
        self.autoflush_like = 0

    def reset(self):
        self.__init__()


C = Counters()


@event.listens_for(engine, "before_cursor_execute")
def _count_sql(conn, cur, stmt, params, ctx, many):
    C.total += 1
    head = " ".join(stmt.split())[:60]
    C.sql[head] += 1
    if "sms_enterprises" in stmt:
        if stmt.lstrip().upper().startswith("INSERT"):
            C.ent_inserts += 1
        else:
            C.ent_selects += 1


@event.listens_for(Session, "before_flush")
def _count_bf(session, ctx, instances):
    C.before_flush += 1


@event.listens_for(Session, "after_flush")
def _count_af(session, ctx):
    C.flush += 1


def summarize(label, n):
    print(
        f"  {label:<34} SQL/op={C.total / n:6.2f}  SELECT sms_enterprises/op={C.ent_selects / n:5.2f}  "
        f"INSERT ent/op={C.ent_inserts / n:4.2f}  before_flush/op={C.before_flush / n:5.2f}  flush/op={C.flush / n:5.2f}"
    )


def with_mode(on: bool):
    settings.enterprise_dual_write = on


# --- 1. request tipik --------------------------------------------------------------------------


def probe_request(keys):
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    owner, key = keys[0]
    print("1) POST /v1/messages (një kërkesë tipike)")
    for on in (False, True):
        with_mode(on)
        for i in range(3):  # ngroh (importe, cache)
            client.post(
                "/v1/messages",
                json={"owner_ref": owner, "to": "+355691230003", "sender": "BENCH0", "text": "w"},
                headers={"Authorization": f"Bearer {key}", "Idempotency-Key": f"warm-{on}-{i}"},
            )
        C.reset()
        n = 60
        for i in range(n):
            r = client.post(
                "/v1/messages",
                json={"owner_ref": owner, "to": "+355691230003", "sender": "BENCH0", "text": "m"},
                headers={"Authorization": f"Bearer {key}", "Idempotency-Key": f"req-{on}-{i}"},
            )
            assert r.status_code == 202, r.text
        summarize("dual-write " + ("ON " if on else "OFF"), n)
        if on:
            extra = [(k, v / n) for k, v in C.sql.items() if "sms_enterprises" in k]
            print("     statement-et me sms_enterprises:", extra)


# --- 2. workeri ----------------------------------------------------------------------------------


def probe_worker(keys, n=200):
    from app.services import messages as svc

    print(
        f"2) worker: {n} mesazhe të radhës, provider fake. Dy forma: (a) një sesion për të gjitha, (b) sesion i ri për cikël (siç bën app/worker.py)"
    )
    owner = keys[1][0]
    calls = Counter()
    import app.services.enterprises as ent

    real = ent.resolve_id

    def counted(db, o):
        calls["resolve_id"] += 1
        return real(db, o)

    ent.resolve_id = counted
    try:
        for form in ("a: një sesion", "b: sesion/cikël"):
            for on in (False, True):
                with_mode(on)
                with SessionLocal() as db:
                    for i in range(n):
                        svc.submit(
                            db, owner, f"wk-{form[0]}-{on}-{i}", "+355691230003", "BENCH1", text="w"
                        )
                        db.commit()
                calls.clear()
                C.reset()
                t0 = time.perf_counter()
                done = 0
                if form[0] == "a":
                    with SessionLocal() as db:
                        while done < n and svc.process_one(db) is not None:
                            done += 1
                else:
                    while done < n:
                        with SessionLocal() as db:
                            if svc.process_one(db) is None:
                                break
                        done += 1
                dt = time.perf_counter() - t0
                summarize(f"({form}) dual-write {'ON ' if on else 'OFF'}", max(done, 1))
                print(
                    f"     {done} mesazhe, {dt:.2f}s ({done / dt:.0f}/s), resolve_id thirrje={calls['resolve_id']} ({calls['resolve_id'] / max(done, 1):.2f}/mesazh)"
                )
    finally:
        ent.resolve_id = real


# --- 3. mikro-matje e shkrimit -------------------------------------------------------------------


def _timeit(fn, n):
    ts = []
    for i in range(n):
        t = time.perf_counter()
        fn(i)
        ts.append((time.perf_counter() - t) * 1e6)
    return ts


def probe_micro(n=1500):
    from app.models.inbound import Keyword

    print(f"3) mikro-matje: krijo objekt tenant-owned + commit ({n} op për variant, μs/op)")

    def stats(label, ts):
        s = sorted(ts)
        print(
            f"  {label:<44} mean {statistics.mean(ts):7.0f}  median {statistics.median(ts):7.0f}  p95 {s[int(len(s) * 0.95)]:7.0f}  min {s[0]:6.0f}"
        )

    def cold(prefix, owner):
        def f(i):  # sesion i ri për çdo op: cache bosh (resolver cold)
            with SessionLocal() as db:
                db.add(Keyword(owner_ref=owner, keyword=f"{prefix}{i}"))
                db.commit()

        return f

    def warm_factory(prefix, owner):
        db = SessionLocal()
        db.add(Keyword(owner_ref=owner, keyword=f"{prefix}seed"))
        db.commit()

        def f(i):  # i njëjti sesion: cache e ngrohtë
            db.add(Keyword(owner_ref=owner, keyword=f"{prefix}{i}"))
            db.commit()

        return f, db

    def newtenant(prefix):
        def f(i):  # owner_ref i ri çdo herë: SELECT + INSERT enterprise + SELECT
            with SessionLocal() as db:
                db.add(Keyword(owner_ref=f"{prefix}{i}", keyword="k"))
                db.commit()

        return f

    rounds = []
    for r in range(3):  # 3 raunde të ndërthurura kundër drift-it
        with_mode(False)
        off = _timeit(cold(f"off{r}_", "bench2"), n // 3)
        with_mode(True)
        c = _timeit(cold(f"cold{r}_", "bench2"), n // 3)
        f, db = warm_factory(f"warm{r}_", "bench2")
        w = _timeit(f, n // 3)
        db.close()
        with_mode(False)
        offw_f, db = warm_factory(f"offw{r}_", "bench2")
        offw = _timeit(offw_f, n // 3)
        db.close()
        rounds.append((off, c, w, offw))
    stats("OFF, sesion i ri për op (baza)", [x for r in rounds for x in r[0]])
    stats("ON,  resolver COLD (sesion i ri për op)", [x for r in rounds for x in r[1]])
    stats("OFF, i njëjti sesion (baza warm)", [x for r in rounds for x in r[3]])
    stats("ON,  resolver WARM (i njëjti sesion)", [x for r in rounds for x in r[2]])
    with_mode(False)
    nt_off = _timeit(newtenant("nto"), 300)
    with_mode(True)
    nt_on = _timeit(newtenant("ntn"), 300)
    stats("OFF, tenant i ri (sesion i ri)", nt_off)
    stats("ON,  tenant i ri (SELECT+INSERT+SELECT)", nt_on)
    with_mode(True)


def main() -> int:
    guard()
    keys = setup(3)
    probe_request(keys)
    probe_worker(keys)
    probe_micro()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    _ = select
    sys.exit(main())
