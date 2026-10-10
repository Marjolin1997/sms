"""Matje e shpejtësisë së një fushate (vetëm baza `*_bench`, pas `scripts.bench`):
    SMS_DATABASE_URL=…/sms_bench … python -m scripts.bench_campaign 3000 10000
Argumentet: numri i marrësve, rate_per_minute. Krijon kontakte, listë, fushatë (transaksionale,
provider fake), nis 2 workers dhe raporton mesazhe/s deri sa fushata përfundon."""

import os
import subprocess
import sys
import time

from sqlalchemy import func, select

from app.core.db import SessionLocal, engine
from app.models.campaigns import Campaign, CampaignRecipient
from app.models.contacts import Contact
from app.services import campaigns, contacts

OWNER = "bench1"


def main(n: int, rate: int) -> int:
    if not engine.url.database.endswith("_bench"):
        sys.exit("refuzohet: vetëm bazë me emër që mbaron me _bench")
    tag = f"{n}-{rate}-{int(time.time())}"
    with SessionLocal() as db:
        t = time.perf_counter()
        lst = contacts.create_list(db, OWNER, f"big{tag}")
        base = db.scalar(select(func.coalesce(func.max(Contact.id), 0))) + 1
        for start in range(0, n, 1000):
            rows = [{"phone": f"+35569{(base + start + i) % 10**7:07d}", "first_name": "X"}
                    for i in range(min(1000, n - start))]  # fmt: skip
            contacts.import_contacts(db, OWNER, rows)
            db.commit()
        ids = list(db.scalars(select(Contact.id).where(Contact.owner_ref == OWNER)
                              .order_by(Contact.id.desc()).limit(n)))  # fmt: skip
        for s in range(0, len(ids), 1000):
            contacts.add_members(db, OWNER, lst.id, ids[s : s + 1000])
        db.commit()
        print(f"përgatitja: {n} kontakte + listë në {time.perf_counter() - t:.1f}s")
        c = campaigns.create(db, OWNER, f"camp{tag}", lst.id, "BENCH1", "bench",
                             text="Hello {{first_name}}", category="transactional",
                             rate_per_minute=rate)  # fmt: skip
        db.commit()
        campaigns.schedule(db, OWNER, c.id, None)
        db.commit()
        cid = c.id
    procs = [
        subprocess.Popen([sys.executable, "-m", "app.worker"],
                         env=os.environ | {"SMS_WORKER_HEARTBEAT": f"/tmp/cb-{i}"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for i in range(2)
    ]  # fmt: skip
    t0 = time.perf_counter()
    try:
        while True:
            with SessionLocal() as db:
                status = db.scalar(select(Campaign.status).where(Campaign.id == cid)).value
                done = db.scalar(select(func.count()).select_from(CampaignRecipient)
                                 .where(CampaignRecipient.campaign_id == cid,
                                        CampaignRecipient.status != "pending"))  # fmt: skip
            print(f"{time.perf_counter() - t0:5.0f}s {status} procesuar={done}", flush=True)
            if status in ("completed", "cancelled", "paused") or time.perf_counter() - t0 > 900:
                break
            time.sleep(10)
    finally:
        for p in procs:
            p.terminate()
    secs = time.perf_counter() - t0
    print(f"fushatë {n} marrës, kufi {rate}/min: {secs:.0f}s ⇒ {n / secs:.0f} mesazhe/s")
    return 0


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]), int(sys.argv[2])))
