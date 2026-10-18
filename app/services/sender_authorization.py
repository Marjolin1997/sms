"""M10-S0: autorizimi KANONIK i sender-it (një vend i vetëm për normalizim, kërkim dhe vendim) — pa Central, pa rrjet.

Semantika (e miratuar):
- Krahasimi i autorizimit është **case-insensitive** për alfanumerikët: çelësi kanonik = `value.lower()`; për numerikët = vetëm shifrat (pa `+`).
  `SenderId.value` ruhet siç u shtyp (display); `norm_value` mban çelësin. `approved_key = "<SHTET>:<norm>"` (UNIQUE global) përdor të njëjtin çelës.
- `check_outbound` bën **një SELECT** (po aq sa para M10) dhe kthen rezultat të strukturuar, jo ORM; i njëjti rezultat ushqen provenancën e `Message`.
- `has_approved_sender` (schedule i fushatës) është VETËM kontroll pa shtet: s'shpik shtet. Autorizimi përfundimtar me shtet bëhet kur ndërtohet submit-i i marrësit.
- `owners_of_numeric` (SMS hyrës) është metodë e ndarë, por mbi të njëjtën gjendje `SenderId` dhe të njëjtin normalizim.
- `recheck_for_dispatch` është API e PËRGATITUR për rikontrollin para dërgimit (vendim B): NUK është lidhur me `process_one` në S0."""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import worker_owner
from app.core.errors import DomainError
from app.core.scope import Owner, owned
from app.models.messaging import ApprovalStatus, SenderId, SenderKind

ALNUM = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9 ]{3,11}$")
NUMERIC = re.compile(r"^\+?[1-9]\d{2,14}$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")

# kategoritë e arsyes (të qëndrueshme; jo tekst i lirë)
OK, NOT_FOUND, PENDING, REJECTED, REVOKED = (
    "approved",
    "not_found",
    "pending",
    "rejected",
    "revoked",
)


class SenderNotAllowed(DomainError):
    code = "sender_not_allowed"


class InvalidSender(DomainError):
    code = "invalid_sender"


@dataclass(frozen=True, slots=True)
class Normalized:
    display: str
    kind: SenderKind
    norm: str


@dataclass(frozen=True, slots=True)
class Authorization:
    """Rezultat i strukturuar i autorizimit (pa ORM). `allowed` ⇔ gjendja `approved`."""

    allowed: bool
    category: str  # approved | not_found | pending | rejected | revoked
    country: str
    canonical_key: str
    sender_ref: int | None = None
    status: str | None = None
    decision_ref: int | None = None
    policy_revision: int | None = None  # NULL = vendim lokal para Central


def classify(value: str) -> tuple[str, SenderKind]:
    if NUMERIC.match(value):
        return value.lstrip("+"), SenderKind.NUMERIC
    if ALNUM.match(value) and value == value.strip():
        return value, SenderKind.ALPHANUMERIC
    raise InvalidSender("sender must be 3-11 alphanumerics (with a letter) or a phone number")


def normalize(value: str) -> Normalized:
    """Vlerë e vlefshme → (display, kind, çelës kanonik). Ngre `InvalidSender` për vlerë të pavlefshme."""
    display, kind = classify(value)
    return Normalized(display, kind, display if kind == SenderKind.NUMERIC else display.lower())


def norm_of(value: str) -> str:
    """Çelësi kanonik për KËRKIM (jo validim): numerik → shifrat; çdo gjë tjetër → lowercase. Nuk ngre kurrë."""
    return value.lstrip("+") if NUMERIC.match(value) else value.lower()


def canonical_key(country: str, norm: str) -> str:
    return f"{country.upper()}:{norm}"


def _result(country: str, norm: str, row: SenderId | None) -> Authorization:
    key = canonical_key(country, norm)
    if row is None:
        return Authorization(False, NOT_FOUND, country, key)
    cat = row.status.value if row.status != ApprovalStatus.APPROVED else OK
    return Authorization(
        allowed=row.status == ApprovalStatus.APPROVED,
        category=cat,
        country=country,
        canonical_key=key,
        sender_ref=row.id,
        status=row.status.value,
        decision_ref=row.current_decision_id,
    )


def pick(rows, display: str | None = None) -> SenderId | None:
    """Rreshta të përputhshëm në rasat ekzistuese (legacy: 'Acme' dhe 'ACME'): i miratuari fiton; pastaj përputhja e saktë; pastaj më i vjetri."""
    rows = sorted(rows, key=lambda r: r.id)
    for r in rows:
        if r.status == ApprovalStatus.APPROVED:
            return r
    if display is not None:
        for r in rows:
            if r.value == display:
                return r
    return rows[0] if rows else None


def check_outbound(db: Session, owner: Owner, country: str, value: str) -> Authorization:
    """Autorizimi i dërgimit për (tenant, shtet, sender). NJË SELECT; case-insensitive; kthen rezultat të strukturuar."""
    country = country.upper()
    norm = norm_of(value)
    rows = db.scalars(
        select(SenderId).where(
            owned(SenderId, owner), SenderId.country == country, SenderId.norm_value == norm
        )
    ).all()
    return _result(country, norm, pick(rows))


def assert_outbound(db: Session, owner: Owner, country: str, value: str) -> Authorization:
    auth = check_outbound(db, owner, country, value)
    if not auth.allowed:
        raise SenderNotAllowed("sender id is not approved for this account and country")
    return auth


def has_approved_sender(db: Session, owner: Owner, value: str) -> bool:
    """Schedule i fushatës: kontroll pa shtet (një sender i miratuar në ≥1 shtet). Autorizimi me shtet bëhet në submit-in e marrësit."""
    return (
        db.scalar(
            select(SenderId.id)
            .where(
                owned(SenderId, owner),
                SenderId.norm_value == norm_of(value),
                SenderId.status == ApprovalStatus.APPROVED,
            )
            .limit(1)
        )
        is not None
    )


def owners_of_numeric(db: Session, number: str) -> list:
    """Kush ka miratuar këtë numër si sender (SMS hyrës → STOP/START). Identiteti i tenant-it vjen nga rreshti `SenderId`, jo nga kërkesa."""
    norm = number.lstrip("+")
    rows = db.scalars(
        select(SenderId).where(
            SenderId.norm_value == norm,
            SenderId.kind == SenderKind.NUMERIC,
            SenderId.status == ApprovalStatus.APPROVED,
        )
    )
    return list({r.owner_ref: worker_owner(db, r) for r in rows}.values())


def recheck_for_dispatch(db: Session, sender_ref: int | None) -> Authorization | None:
    """PËRGATITUR (S0, e pa-lidhur): gjendja AKTUALE e sender-it të ngrirë në mesazh. `None` = mesazh pa provenancë (para M10) ⇒ pa rikontroll.
    Semantika e synuar B (aktivizim vetëm pas miratimit të veçantë): approved → vazhdo; revoked/rejected/pending/mungon → dështim i mbyllur me
    `sender_revoked` + çlirim i hold-it; asnjë thirrje rrjeti; një SELECT sipas PK."""
    if sender_ref is None:
        return None
    row = db.get(SenderId, sender_ref)
    if row is None:
        return Authorization(False, NOT_FOUND, "", "", sender_ref=sender_ref)
    return _result(row.country, row.norm_value, row)
