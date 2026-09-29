import base64
import re
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.models.email import DomainStatus, EmailDomain
from app.services.dns_check import DnsError, get_resolver
from app.services.wallet import Conflict, NotFound, WalletError

_DOMAIN = re.compile(r"^(?=.{4,190}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
SELECTOR = "sms1"


class InvalidDomain(WalletError):
    code = "invalid_domain"


def encrypt(pem: bytes) -> str:
    return crypto.encrypt(pem)


def decrypt_private_key(d: EmailDomain) -> bytes:
    return crypto.decrypt(d.dkim_private_key_enc)


def create(db: Session, owner_ref: str, domain: str) -> EmailDomain:
    domain = domain.strip().lower().rstrip(".")
    if not _DOMAIN.match(domain):
        raise InvalidDomain("invalid domain name")
    if db.scalar(
        select(EmailDomain).where(EmailDomain.owner_ref == owner_ref, EmailDomain.domain == domain)
    ):
        raise Conflict("domain already added")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    d = EmailDomain(
        owner_ref=owner_ref, domain=domain, dkim_selector=SELECTOR,
        dkim_public_key=base64.b64encode(pub).decode(), dkim_private_key_enc=encrypt(pem),
    )  # fmt: skip
    db.add(d)
    db.flush()
    return d


def dns_records(d: EmailDomain) -> list[dict]:
    """Rekordet që klienti duhet t'i publikojë."""
    return [
        {"type": "TXT", "name": f"{d.dkim_selector}._domainkey.{d.domain}",
         "value": f"v=DKIM1; k=rsa; p={d.dkim_public_key}", "required": True},
        {"type": "TXT", "name": d.domain,
         "value": f"v=spf1 include:{settings.spf_include} ~all", "required": True,
         "note": "nëse ke SPF ekzistues, shtoji vetëm include:" + settings.spf_include},
        {"type": "TXT", "name": f"_dmarc.{d.domain}",
         "value": f"v=DMARC1; p=none; rua=mailto:dmarc@{d.domain}", "required": False},
    ]  # fmt: skip


def _tags(record: str) -> dict[str, str]:
    out = {}
    for part in record.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def verify(db: Session, owner_ref: str, domain_id: int) -> EmailDomain:
    """Kontrollon DNS. Verifikohet vetëm me DKIM (provë kontrolli mbi domenin) dhe SPF."""
    d = db.scalar(
        select(EmailDomain)
        .where(EmailDomain.id == domain_id, EmailDomain.owner_ref == owner_ref)
        .with_for_update()
    )
    if d is None:
        raise NotFound("domain not found")
    r = get_resolver()
    try:
        dkim = r.txt(f"{d.dkim_selector}._domainkey.{d.domain}")
        spf = r.txt(d.domain)
        dmarc = r.txt(f"_dmarc.{d.domain}")
    except DnsError as e:
        raise Conflict(f"DNS lookup failed, try again later: {e}") from e
    d.dkim_ok = any(
        _tags(t).get("p", "").replace(" ", "") == d.dkim_public_key and _tags(t).get("v") == "DKIM1"
        for t in dkim
    )
    d.spf_ok = any(
        t.lower().startswith("v=spf1") and f"include:{settings.spf_include}".lower() in t.lower()
        for t in spf
    )
    d.dmarc_ok = any(t.lower().startswith("v=dmarc1") for t in dmarc)
    d.last_checked_at = datetime.now(UTC)
    if d.dkim_ok and d.spf_ok:
        if d.status != DomainStatus.VERIFIED:
            d.status, d.verified_at, d.verified_key = (
                DomainStatus.VERIFIED,
                d.last_checked_at,
                d.domain,
            )
            try:
                db.flush()
            except IntegrityError as e:
                db.rollback()
                raise Conflict("domain is already verified by another account") from e
    else:  # DNS u hoq: nuk lejojmë më dërgim nga ky domen
        d.status, d.verified_at, d.verified_key = DomainStatus.PENDING, None, None
    db.flush()
    return d


def verified_domain_for(db: Session, owner_ref: str, from_email: str) -> EmailDomain | None:
    domain = from_email.rsplit("@", 1)[-1].lower()
    return db.scalar(
        select(EmailDomain).where(
            EmailDomain.owner_ref == owner_ref,
            EmailDomain.domain == domain,
            EmailDomain.status == DomainStatus.VERIFIED,
        )
    )
