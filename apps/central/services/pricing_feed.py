"""Feed-i `cp.pricing.v1` (M9-e): SNAPSHOT i plotë i çmimeve për enterprise-et e autorizuara të klientit, vetëm-lexim.

Lexohet në NJË transaksion REPEATABLE READ (PG): `revision` dhe përmbajtja janë nga e njëjta pamje (çdo mutacion çmimi e rrit
`revision` në të njëjtin commit). Konsumatori i verifikon `snapshot_hash` + `content_hash` per version: snapshot i paplotë nuk
aktivizohet. `changed=false` kur (epoch, revision, authorization_generation) përputhen me ato që konsumatori ka aplikuar.
Drafte KURRË nuk dërgohen. Çmimi i klientit vetëm (pa kosto provider)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.models.pricing import V_DRAFT, PriceAssignment, PriceBook, PriceRule, PriceVersion
from apps.central.services import pricing, service_auth
from packages.contracts.control_plane.pricing import v1 as pv


def state(db: Session, client_pk) -> dict:
    epoch, revision = pricing.read_state(db)
    generation, _ = service_auth.allowed_enterprises(db, client_pk)
    return {"epoch": str(epoch), "revision": revision, "authorization_generation": generation}


def build(db: Session, client_pk) -> pv.PricingSnapshotV1:
    epoch, revision = pricing.read_state(db)  # i pari, brenda snapshot-it
    generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    assignments = list(db.scalars(select(PriceAssignment).where(PriceAssignment.enterprise_id.in_(allowed))
                                  .order_by(PriceAssignment.effective_from, PriceAssignment.id))) if allowed else []  # fmt: skip
    book_ids = {a.price_book_id for a in assignments}
    books_out = []
    for b in (
        db.scalars(select(PriceBook).where(PriceBook.id.in_(book_ids)).order_by(PriceBook.id))
        if book_ids
        else []
    ):
        versions = []
        for v in db.scalars(select(PriceVersion).where(PriceVersion.price_book_id == b.id, PriceVersion.status != V_DRAFT)
                            .order_by(PriceVersion.effective_from)):  # fmt: skip
            rules = [{"rule_id": str(r.id), "channel": r.channel, "prefix": r.prefix, "operator": r.operator,
                      "unit_price": pv.format_price(r.unit_price)}
                     for r in db.scalars(select(PriceRule).where(PriceRule.version_id == v.id))]  # fmt: skip
            versions.append({"version_id": str(v.id), "version": v.version, "status": v.status,
                             "effective_from": pv.format_ts(v.effective_from), "content_hash": v.content_hash,
                             "rules": rules})  # fmt: skip
        books_out.append(
            {"book_id": str(b.id), "code": b.code, "currency": b.currency, "versions": versions}
        )
    by_ent: dict[uuid.UUID, list[dict]] = {e: [] for e in allowed}
    for a in assignments:
        by_ent[a.enterprise_id].append({"assignment_id": str(a.id), "product_id": str(a.product_id),
                                        "price_book_id": str(a.price_book_id), "effective_from": pv.format_ts(a.effective_from)})  # fmt: skip
    enterprises = [{"enterprise_id": str(e), "assignments": a} for e, a in by_ent.items()]
    return pv.PricingSnapshotV1.build(epoch=str(epoch), revision=revision, generation=generation,
                                      enterprises=enterprises, books=books_out)  # fmt: skip


def changes(
    db: Session,
    client_pk,
    known_epoch: str | None,
    known_revision: int | None,
    known_generation: int | None,
) -> dict:
    epoch, revision = pricing.read_state(db)
    generation, _ = service_auth.allowed_enterprises(db, client_pk)
    if (known_epoch, known_revision, known_generation) == (str(epoch), revision, generation):
        return {
            "changed": False,
            "epoch": str(epoch),
            "revision": revision,
            "authorization_generation": generation,
        }
    return {"changed": True, "snapshot": build(db, client_pk).to_dict()}
