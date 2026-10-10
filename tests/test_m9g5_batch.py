# ruff: noqa: F811
"""M9-g5 — batch faturimi mbi dataset testimi: saktësi (një faturë për abonim, numra të njëpasnjëshëm, rerun = 0) dhe kohë (raportohet, kufi i gjerë)."""

import time

from sqlalchemy import func, select

from apps.central.models.billing import Invoice
from apps.central.services import billing, enterprises
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import AFTER1, T0, U, b, mk_plan, profile, subscribe  # noqa: F401

N = 120


def test_batch_of_120_subscriptions_is_exact_idempotent_and_timed(b, capsys):
    vid = mk_plan(b, fee="20")
    with b.F() as s:
        eids = [enterprises.create(s, f"Ent {i}").id for i in range(N)]
        s.commit()
    for e in eids:
        profile(b, e)
        subscribe(b, vid, e, at=T0)
    t0 = time.perf_counter()
    out = billing.run_exclusive(b.eng, AFTER1)
    dt = time.perf_counter() - t0
    assert (out.invoiced, out.failed) == (N, 0)
    again = billing.run_exclusive(b.eng, AFTER1)
    assert again.invoiced == 0 and again.failed == 0
    with b.F() as s:
        nums = sorted(s.scalars(select(Invoice.number)))
        assert len(nums) == N and len(set(nums)) == N
        seq = [int(x.rsplit("-", 1)[1]) for x in nums]
        assert seq == list(range(1, N + 1))  # pa boshllëqe
        assert s.scalar(select(func.count()).select_from(Invoice)) == N
    with capsys.disabled():
        print(
            f"\n[m9g5 batch timing] {N} subscriptions invoiced in {dt:.2f}s ({dt / N * 1000:.1f} ms/period) on {b.eng.dialect.name}"
        )  # noqa: T201
    assert dt < 120  # kufi i gjerë (mjedis i ngarkuar); numri real raportohet më lart
