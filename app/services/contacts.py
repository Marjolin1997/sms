from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models.contacts import (
    Contact,
    ContactList,
    ContactStatus,
    ListMember,
)
from app.services import consent
from app.services.wallet import Conflict, NotFound, WalletError

MAX_IMPORT = 1000


class InvalidContact(WalletError):
    code = "invalid_contact"


def _attrs(attrs: dict | None) -> dict | None:
    if not attrs:
        return None
    if len(attrs) > 20:
        raise InvalidContact("at most 20 attributes")
    for k, v in attrs.items():
        if not isinstance(k, str) or not 1 <= len(k) <= 32:
            raise InvalidContact("attribute names must be 1-32 chars")
        if not isinstance(v, str | int | float | bool) or len(str(v)) > 200:
            raise InvalidContact(f"attribute '{k}' must be a scalar of at most 200 chars")
    return attrs


def _norm(phone: str | None, email: str | None) -> tuple[str | None, str | None]:
    try:
        p = consent.normalize("sms", phone) if phone else None
        e = consent.normalize("email", email) if email else None
    except consent.InvalidAddress as ex:
        raise InvalidContact(str(ex)) from ex
    if not p and not e:
        raise InvalidContact("phone or email is required")
    return p, e


def _get(db: Session, owner_ref: str, contact_id: int, lock: bool = False) -> Contact:
    q = select(Contact).where(
        Contact.id == contact_id,
        Contact.owner_ref == owner_ref,
        Contact.status == ContactStatus.ACTIVE,
    )
    c = db.scalar(q.with_for_update() if lock else q)
    if c is None:
        raise NotFound("contact not found")
    return c


def upsert(
    db: Session,
    owner_ref: str,
    phone: str | None = None,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    attributes: dict | None = None,
    external_id: str | None = None,
) -> tuple[Contact, bool]:
    """Krijon ose përditëson sipas phone/email/external_id. → (contact, created)."""
    p, e = _norm(phone, email)
    attributes = _attrs(attributes)
    matches: dict[int, Contact] = {}
    for col, val in (
        (Contact.phone, p),
        (Contact.email, e),
        (Contact.external_id, external_id),
    ):
        if val:
            c = db.scalar(
                select(Contact).where(
                    Contact.owner_ref == owner_ref,
                    col == val,
                    Contact.status == ContactStatus.ACTIVE,
                )
            )
            if c:
                matches[c.id] = c
    if len(matches) > 1:
        raise Conflict("phone, email and external_id belong to different contacts")
    now = datetime.now(UTC)
    if matches:
        c = next(iter(matches.values()))
        # një adresë e re nuk mbishkruan një ekzistuese (kjo do të ishte ndryshim identiteti)
        if (c.phone and p and c.phone != p) or (c.email and e and c.email != e):
            raise Conflict("contact already has a different phone/email")
        c.phone, c.email = c.phone or p, c.email or e
        for k, v in (("first_name", first_name), ("last_name", last_name)):
            if v is not None:
                setattr(c, k, v[:64])
        if attributes is not None:
            c.attributes = {**(c.attributes or {}), **attributes}
        c.external_id = c.external_id or external_id
        c.updated_at = now
        db.flush()
        return c, False
    c = Contact(
        owner_ref=owner_ref, phone=p, email=e, external_id=external_id,
        first_name=first_name and first_name[:64], last_name=last_name and last_name[:64],
        attributes=attributes,
    )  # fmt: skip
    db.add(c)
    db.flush()
    return c, True


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    errors: list[dict] = field(default_factory=list)


def import_contacts(db: Session, owner_ref: str, rows: list[dict]) -> ImportResult:
    """Çdo rresht në savepoint: një rresht i keq nuk prish të tjerët."""
    if len(rows) > MAX_IMPORT:
        raise InvalidContact(f"at most {MAX_IMPORT} rows per import")
    out = ImportResult()
    for i, row in enumerate(rows):
        try:
            with db.begin_nested():
                _, created = upsert(db, owner_ref, **row)
            out.created += created
            out.updated += not created
        except WalletError as e:
            out.errors.append({"row": i, "code": e.code, "message": str(e)})
    return out


def update(db: Session, owner_ref: str, contact_id: int, **fields) -> Contact:
    c = _get(db, owner_ref, contact_id, lock=True)
    if "attributes" in fields and fields["attributes"] is not None:
        c.attributes = _attrs(fields["attributes"])
    for k in ("first_name", "last_name"):
        if fields.get(k) is not None:
            setattr(c, k, fields[k][:64])
    c.updated_at = datetime.now(UTC)
    db.flush()
    return c


def erase(db: Session, owner_ref: str, contact_id: int, actor: str) -> Contact:
    """GDPR: PII fshihet, anëtarësitë hiqen; adresat mbeten të bllokuara vetëm si HMAC,
    që një import i mëvonshëm të mos i rikthejë në dërgim."""
    c = _get(db, owner_ref, contact_id, lock=True)
    for channel, addr in (("sms", c.phone), ("email", c.email)):
        if addr:
            consent.record(
                db, owner_ref, channel, addr, "opt_out", "erasure", "erasure_request", actor
            )
    db.execute(delete(ListMember).where(ListMember.contact_id == c.id))
    from app.models.campaigns import CampaignRecipient, RecipientStatus

    db.execute(  # kopja e adresës te marrësit e campaign-eve hiqet; të pa-dërguarit anulohen
        sa_update(CampaignRecipient)
        .where(CampaignRecipient.contact_id == c.id)
        .values(address=None)
    )
    db.execute(
        sa_update(CampaignRecipient)
        .where(
            CampaignRecipient.contact_id == c.id,
            CampaignRecipient.status == RecipientStatus.PENDING,
        )
        .values(status=RecipientStatus.CANCELLED, reason="contact_erased")
    )
    c.phone = c.email = c.first_name = c.last_name = c.attributes = c.external_id = None
    c.status = ContactStatus.ERASED
    c.updated_at = datetime.now(UTC)
    db.flush()
    return c


# --- Lista ------------------------------------------------------------------


def create_list(db: Session, owner_ref: str, name: str) -> ContactList:
    if db.scalar(
        select(ContactList).where(ContactList.owner_ref == owner_ref, ContactList.name == name)
    ):
        raise Conflict("list name already exists")
    lst = ContactList(owner_ref=owner_ref, name=name)
    db.add(lst)
    db.flush()
    return lst


def _list(db: Session, owner_ref: str, list_id: int) -> ContactList:
    lst = db.scalar(
        select(ContactList).where(ContactList.id == list_id, ContactList.owner_ref == owner_ref)
    )
    if lst is None:
        raise NotFound("list not found")
    return lst


def add_members(db: Session, owner_ref: str, list_id: int, contact_ids: list[int]) -> int:
    _list(db, owner_ref, list_id)
    if len(contact_ids) > MAX_IMPORT:
        raise InvalidContact(f"at most {MAX_IMPORT} contacts per call")
    valid = set(
        db.scalars(
            select(Contact.id).where(
                Contact.owner_ref == owner_ref,
                Contact.id.in_(contact_ids),
                Contact.status == ContactStatus.ACTIVE,
            )
        )
    )
    if valid != set(contact_ids):
        raise NotFound("some contacts were not found")
    existing = set(
        db.scalars(
            select(ListMember.contact_id).where(
                ListMember.list_id == list_id, ListMember.contact_id.in_(valid)
            )
        )
    )
    for cid in valid - existing:
        db.add(ListMember(list_id=list_id, contact_id=cid))
    db.flush()
    return len(valid - existing)


def remove_member(db: Session, owner_ref: str, list_id: int, contact_id: int) -> None:
    _list(db, owner_ref, list_id)
    db.execute(
        delete(ListMember).where(ListMember.list_id == list_id, ListMember.contact_id == contact_id)
    )


# --- Audienca (për campaigns) ---------------------------------------------------


@dataclass(frozen=True)
class AudienceRow:
    contact_id: int
    address: str | None
    allowed: bool
    reason: str  # ok | no_address | no_consent | opted_out | blocked:<arsyeja>


def audience_batch(
    db: Session,
    owner_ref: str,
    list_id: int,
    channel: str,
    category: str,
    after_id: int = 0,
    limit: int = 500,
) -> list[AudienceRow]:
    """Faqe e audiencës me vendim për secilin kontakt. Campaigns e thërrasin me `after_id`
    që të mos ngarkojnë listën e plotë në memorie."""
    _list(db, owner_ref, list_id)
    contacts = db.scalars(
        select(Contact)
        .join(ListMember, ListMember.contact_id == Contact.id)
        .where(
            ListMember.list_id == list_id,
            Contact.id > after_id,
            Contact.status == ContactStatus.ACTIVE,
        )
        .order_by(Contact.id)
        .limit(limit)
    ).all()
    from app.models.contacts import ConsentState

    hashes: dict[int, str] = {}
    for c in contacts:
        addr = c.phone if channel == "sms" else c.email
        if addr:
            hashes[c.id] = consent.address_hash(owner_ref, channel, addr)
    states = {}
    if hashes:
        for st in db.scalars(
            select(ConsentState).where(
                ConsentState.owner_ref == owner_ref,
                ConsentState.channel == channel,
                ConsentState.address_hash.in_(set(hashes.values())),
            )
        ):
            states[st.address_hash] = st
    rows = []
    for c in contacts:
        addr = c.phone if channel == "sms" else c.email
        if not addr:
            rows.append(AudienceRow(c.id, None, False, "no_address"))
            continue
        d = consent.decide(states.get(hashes[c.id]), category)
        rows.append(AudienceRow(c.id, addr, d.allowed, d.reason))
    return rows


def audience_counts(
    db: Session, owner_ref: str, list_id: int, channel: str, category: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    after = 0
    while True:
        batch = audience_batch(db, owner_ref, list_id, channel, category, after)
        if not batch:
            return counts
        for r in batch:
            counts[r.reason] = counts.get(r.reason, 0) + 1
        after = batch[-1].contact_id
