"""Regjistri i Enterprise-ve: zgjidhja `owner_ref → enterprise.id` (M1b/M1c); shtresa e modeleve.

Primitiva e persistencës (SELECT / INSERT … ON CONFLICT DO NOTHING mbi `sms_enterprises`) që
`core.context` dhe `core.tenancy` e kërkojnë; s'ka logjikë biznesi, auditim apo backfill (ato mbeten
te `services.enterprises`, që e ri-eksporton këtë modul për përputhshmëri). Varet vetëm nga
`models.enterprise`/`models.tenant`: asnjë import nga shtresa e shërbimeve.

Rregull: `owner_ref` krahasohet saktësisht siç është. Asnjë normalizim, bashkim apo hamendësim."""

import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models.enterprise import Enterprise
from app.models.tenant import TenantOwned

# Tabelat që mbajnë `owner_ref` direkt. Kopja e ngrirë e kësaj liste është te migrimi 0018;
# testi i driftit i detyron të përputhen me modelet.
LEGACY_OWNER_TABLES = (
    "sms_account_plans", "sms_api_keys", "sms_billing_profiles", "sms_campaigns",
    "sms_consent_events", "sms_consent_state", "sms_contact_lists", "sms_contacts",
    "sms_email_domains", "sms_emails", "sms_events", "sms_inbound_messages", "sms_invoices",
    "sms_keywords", "sms_messages", "sms_payments", "sms_sender_ids", "sms_subscriptions",
    "sms_templates", "sms_wallets", "sms_webhook_endpoints",
)  # fmt: skip
MAX_LEN = 64
log = logging.getLogger("sms.enterprises")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def valid_owner_ref(owner_ref) -> bool:
    return (
        isinstance(owner_ref, str)
        and owner_ref != ""
        and owner_ref == owner_ref.strip()
        and len(owner_ref) <= MAX_LEN
        and not _CONTROL.search(owner_ref)
    )


def _insert_if_absent(db: Session, owner_ref: str) -> None:
    """INSERT … ON CONFLICT DO NOTHING (i sigurt në konkurrencë; pa përjashtim nëse dy transaksione
    krijojnë të njëjtin tenant njëkohësisht, ose nëse një variant shkronjash e përplas indeksin)."""
    now = datetime.now(UTC)
    values = {
        "id": uuid.uuid4(), "owner_ref": owner_ref, "status": "active",
        "created_at": now, "updated_at": now,
    }  # fmt: skip
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as ins
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as ins
    else:  # dialekte të tjerë: savepoint + kapje e IntegrityError
        from sqlalchemy.exc import IntegrityError

        try:
            with db.begin_nested():
                db.add(Enterprise(**values))
                db.flush()
        except IntegrityError:
            pass
        return
    db.execute(ins(Enterprise.__table__).values(**values).on_conflict_do_nothing())


def lookup_id(db: Session, owner_ref) -> uuid.UUID | None:
    """`enterprise.id` vetëm-lexim (M1c): nuk krijon asgjë. `None` nëse mungon ose është anomali."""
    if not valid_owner_ref(owner_ref):
        return None
    cache: dict[str, uuid.UUID | None] = db.info.setdefault("_enterprise_ids", {})
    if cache.get(owner_ref) is not None:
        return cache[owner_ref]
    with db.no_autoflush:
        found = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == owner_ref))
    if found is not None:
        cache[owner_ref] = found
    return found


def _from_loaded_rows(db: Session, owner_ref: str) -> uuid.UUID | None:
    """`enterprise_id` nga një rresht tenant-owned i të njëjtit `owner_ref` që sesioni e ka tashmë
    të ngarkuar (p.sh. Message që workeri sapo e lexoi, kur krijon Event). Mbështetet te invarianti
    `record.owner_ref == enterprise.owner_ref` (i verifikuar nga `enterprises_audit --check`)."""
    for (
        obj
    ) in db.identity_map.values():  # vetëm rreshta të ruajtur (identity_map nuk përmban të rinj)
        if not isinstance(obj, TenantOwned) or obj.enterprise_id is None:
            continue
        if obj.owner_ref != owner_ref:
            continue
        st = inspect(obj)
        if st.attrs.owner_ref.history.has_changes() or st.attrs.enterprise_id.history.has_changes():
            continue  # owner_ref/enterprise_id në ndryshim e sipër: jo burim i besueshëm
        return obj.enterprise_id
    return None


def resolve_id(db: Session, owner_ref) -> uuid.UUID | None:
    """`enterprise.id` për një `owner_ref` të saktë; krijon Enterprise-in nëse mungon (tenant i ri).
    Kthen `None` (pa përjashtim, pa krijuar asgjë) kur `owner_ref` është anomali: bosh, me hapësira,
    karaktere kontrolli, >64, ose ndryshon vetëm nga shkronjat e një ekzistuesi. Sjellja e sistemit
    nuk ndryshon: rreshti ruhet me `enterprise_id` NULL dhe kontrolli i konsistencës e raporton."""
    if owner_ref is None:
        return None
    if not valid_owner_ref(owner_ref):
        log.warning("owner_ref anomaly, enterprise_id left NULL: %r", owner_ref)
        return None
    cache: dict[str, uuid.UUID | None] = db.info.setdefault("_enterprise_ids", {})
    if owner_ref in cache and cache[owner_ref] is not None:
        return cache[owner_ref]
    known = _from_loaded_rows(db, owner_ref)
    if known is not None:  # rresht i njëjtit tenant, tashmë i ngarkuar në sesion: pa SELECT
        cache[owner_ref] = known
        return known
    with db.no_autoflush:
        found = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == owner_ref))
        if found is None:
            _insert_if_absent(db, owner_ref)
            found = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == owner_ref))
    if found is None:
        log.warning(
            "owner_ref %r conflicts with an existing enterprise variant; enterprise_id NULL",
            owner_ref,
        )
        return None
    cache[owner_ref] = found
    return found
