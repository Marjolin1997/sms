"""Profil cProfile i ciklit të workerit (sesion i ri për cikël), dual-write ON vs OFF,
vetëm mbi bazë `*_bench`.

SMS_DATABASE_URL=…/prof_bench python -m scripts.bench_profile [N]"""

import cProfile
import io
import pstats
import sys
import time

from app.core.config import settings
from app.core.db import SessionLocal
from scripts.bench import guard, setup


def seed(owner, tag, n):
    from app.services import messages as svc

    with SessionLocal() as db:
        for i in range(n):
            svc.submit(db, owner, f"{tag}-{i}", "+355691230003", "BENCH0", text="w")
            db.commit()


def drain(n):
    from app.services import messages as svc

    done = 0
    while done < n:
        with SessionLocal() as db:
            if svc.process_one(db) is None:
                break
        done += 1
    return done


def main() -> int:
    guard()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    owner = setup(1)[0][0]
    res = {}
    for on in (False, True, False, True):
        settings.enterprise_dual_write = on
        seed(owner, f"p{on}{time.time_ns()}", n)
        pr = cProfile.Profile()
        t0 = time.perf_counter()
        pr.enable()
        done = drain(n)
        pr.disable()
        dt = time.perf_counter() - t0
        mode = "ON " if on else "OFF"
        print(f"dual-write {mode}: {done} msg, {dt:.2f}s, {done / dt:.0f}/s (me cProfile)")
        res.setdefault(on, []).append(pr)
    for on in (False, True):
        s = io.StringIO()
        st = pstats.Stats(res[on][-1], stream=s).sort_stats("cumulative")
        st.print_stats(r"tenancy|enterprises|no_autoflush|before_flush|_dual_write", 12)
        print(f"===== {'ON' if on else 'OFF'}: funksionet e dual-write =====")
        print(s.getvalue()[-2500:])
    for on in (False, True):
        s = io.StringIO()
        pstats.Stats(res[on][-1], stream=s).sort_stats("tottime").print_stats(14)
        print(f"===== {'ON' if on else 'OFF'}: top tottime =====")
        print(s.getvalue()[-3500:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
