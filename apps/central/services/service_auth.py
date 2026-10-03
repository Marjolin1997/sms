"""Autentikimi shërbim-te-shërbim: client assertion JWT i nënshkruar me Ed25519 (EdDSA).

Claims të detyrueshme: iss = sub = client_id, aud = `sms-central-sync`, iat, exp (exp-iat <= 300 s),
jti (përdorim një herë), scope (hapësirë-ndarë; duhet të përfshijë scope-in e kërkuar dhe klienti ta ketë).
Header: alg=EdDSA, kid. Central ruan vetëm çelësa publikë (disa per klient: rotacion).
Replay: `jti` ruhet në `service_assertion_jti` (unik per klient) në transaksion të veçantë që mbetet edhe
kur kërkesa dështon më vonë; pastrimi i të skaduarve bëhet lazy në çdo shkrim (pa worker). Kosto: një
shkrim DB per kërkesë; avantazh: funksionon mes procesesh/instancash pa Redis.
Pa logim të token-it; vetëm client id, kid, arsye e sigurt, IP.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.service_auth import (
    ALLOWED_SCOPES,
    CredentialStatus,
    ServiceAssertionJti,
    ServiceClient,
    ServiceClientEnterprise,
    ServiceKey,
)

AUDIENCE = "sms-central-sync"
ALGORITHM = "EdDSA"
MAX_LIFETIME_S = 300
LEEWAY_S = 5
log = logging.getLogger("central.sync_auth")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_JTI = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class ServiceAuthError(Exception):
    """401 gjenerik; `reason` vetëm për log."""

    def __init__(self, reason: str, client_id: str | None = None, kid: str | None = None):
        super().__init__(reason)
        self.reason, self.client_id, self.kid = reason, client_id, kid


class ScopeDenied(ServiceAuthError):
    """403: identiteti është i vlefshëm por nuk ka scope-in."""


@dataclass(frozen=True)
class ServiceContext:
    client_pk: object
    client_id: str
    kid: str
    scopes: frozenset[str]


# --- menaxhimi (CLI/service) ---------------------------------------------------------------------------


def _check_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        raise Invalid(f"{field} must match [A-Za-z0-9][A-Za-z0-9._:-]{{0,63}}")
    return value


def normalize_public_key(pem: str | bytes) -> str:
    """Pranon vetëm Ed25519 publik (PEM SubjectPublicKeyInfo); kthen PEM-in e normalizuar."""
    try:
        key = serialization.load_pem_public_key(pem.encode() if isinstance(pem, str) else pem)
    except (ValueError, TypeError) as e:
        raise Invalid("public key must be a PEM-encoded Ed25519 public key") from e
    if not isinstance(key, Ed25519PublicKey):
        raise Invalid("public key must be an Ed25519 key")
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def _scopes(scopes) -> list[str]:
    values = sorted(set(scopes or ["sync:read"]))
    if not values or not set(values) <= ALLOWED_SCOPES:
        raise Invalid(f"scopes must be a subset of {sorted(ALLOWED_SCOPES)}")
    return values


def create_client(
    db: Session, client_id: str, scopes=None, enterprise_ids=(), *, now: datetime | None = None
) -> ServiceClient:
    _check_id(client_id, "client_id")
    if db.scalar(select(ServiceClient.id).where(ServiceClient.client_id == client_id)):
        raise Conflict("service client already exists")
    now = now or utcnow()
    client = ServiceClient(client_id=client_id, scopes=_scopes(scopes), auth_generation=1,
                           created_at=now, updated_at=now)  # fmt: skip
    db.add(client)
    db.flush()
    for eid in enterprise_ids:
        _grant(db, client, eid, now)
    return client


def get_client(db: Session, client_id: str) -> ServiceClient:
    client = db.scalar(select(ServiceClient).where(ServiceClient.client_id == client_id))
    if client is None:
        raise NotFound("service client not found")
    return client


def add_key(
    db: Session, client_id: str, kid: str, public_key: str | bytes, *, now: datetime | None = None
) -> tuple[ServiceKey, bool]:
    """→ (çelësi, i_ri). Njëjtë kid+çelës = no-op; kid ekzistues me çelës tjetër → Conflict."""
    client = get_client(db, client_id)
    _check_id(kid, "kid")
    pem = normalize_public_key(public_key)
    existing = db.scalar(
        select(ServiceKey).where(ServiceKey.client_pk == client.id, ServiceKey.kid == kid)
    )
    if existing is not None:
        if existing.public_key != pem:
            raise Conflict("kid already exists with a different public key")
        return existing, False
    now = now or utcnow()
    key = ServiceKey(client_pk=client.id, kid=kid, public_key=pem, created_at=now, updated_at=now)
    db.add(key)
    db.flush()
    return key, True


def _grant(db: Session, client: ServiceClient, enterprise_id, now) -> bool:
    if db.get(Enterprise, enterprise_id) is None:
        raise NotFound("enterprise not found")
    if db.get(ServiceClientEnterprise, (client.id, enterprise_id)) is not None:
        return False  # no-op: pa bump të generation
    db.add(
        ServiceClientEnterprise(client_pk=client.id, enterprise_id=enterprise_id, created_at=now)
    )
    client.auth_generation += 1  # bashkësia ndryshoi → konsumatori duhet snapshot
    client.updated_at = now
    db.flush()
    return True


def grant_enterprise(
    db: Session, client_id: str, enterprise_id, *, now: datetime | None = None
) -> bool:
    return _grant(db, get_client(db, client_id), enterprise_id, now or utcnow())


def revoke_enterprise(
    db: Session, client_id: str, enterprise_id, *, now: datetime | None = None
) -> bool:
    client = get_client(db, client_id)
    row = db.get(ServiceClientEnterprise, (client.id, enterprise_id))
    if row is None:
        return False
    db.delete(row)
    client.auth_generation += 1
    client.updated_at = now or utcnow()
    db.flush()
    return True


def disable_key(db: Session, client_id: str, kid: str) -> bool:
    client = get_client(db, client_id)
    key = db.scalar(
        select(ServiceKey).where(ServiceKey.client_pk == client.id, ServiceKey.kid == kid)
    )
    if key is None:
        raise NotFound("key not found")
    if key.status == CredentialStatus.DISABLED.value:
        return False
    key.status, key.updated_at = CredentialStatus.DISABLED.value, utcnow()
    db.flush()
    return True


def disable_client(db: Session, client_id: str) -> bool:
    client = get_client(db, client_id)
    if client.status == CredentialStatus.DISABLED.value:
        return False
    client.status, client.updated_at = CredentialStatus.DISABLED.value, utcnow()
    db.flush()
    return True


def allowed_enterprises(db: Session, client_pk) -> tuple[int, frozenset]:
    """→ (auth_generation, enterprise ids). Lexohen bashkë (i njëjti transaksion/snapshot)."""
    generation = db.scalar(
        select(ServiceClient.auth_generation).where(ServiceClient.id == client_pk)
    )
    ids = db.scalars(
        select(ServiceClientEnterprise.enterprise_id).where(
            ServiceClientEnterprise.client_pk == client_pk
        )
    )
    return int(generation), frozenset(ids)


# --- autentikimi i kërkesës -------------------------------------------------------------------------------


def _claim_str(claims: dict, name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value:
        raise ServiceAuthError(f"missing_{name}")
    return value


def authenticate(
    session_factory: Callable[[], Session], token: str, required_scope: str
) -> ServiceContext:
    """Verifikon assertion-in; ngrënë `jti`; kthen kontekstin ose ServiceAuthError/ScopeDenied."""
    if not token or len(token) > 4096:
        raise ServiceAuthError("missing_token")
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ServiceAuthError("malformed_token") from None
    kid, iss = header.get("kid"), unverified.get("iss")
    if header.get("alg") != ALGORITHM:
        raise ServiceAuthError("bad_algorithm", iss if isinstance(iss, str) else None)
    if not isinstance(kid, str) or not _ID.match(kid):
        raise ServiceAuthError("missing_kid", iss if isinstance(iss, str) else None)
    if not isinstance(iss, str) or not _ID.match(iss):
        raise ServiceAuthError("missing_iss", None, kid)
    with session_factory() as db:
        row = db.execute(
            select(ServiceClient, ServiceKey)
            .join(ServiceKey, ServiceKey.client_pk == ServiceClient.id)
            .where(ServiceClient.client_id == iss, ServiceKey.kid == kid)
        ).first()
        if row is None:
            raise ServiceAuthError("unknown_client_or_kid", iss, kid)
        client, key = row
        if (client.status, key.status) != ("active", "active"):
            raise ServiceAuthError("disabled", iss, kid)
        client_pk, scopes, pem = client.id, frozenset(client.scopes), key.public_key
    try:
        claims = jwt.decode(
            token, pem, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=iss, leeway=LEEWAY_S,
            options={"require": ["exp", "iat", "iss", "sub", "aud", "jti", "scope"]},
        )  # fmt: skip
    except jwt.ExpiredSignatureError:
        raise ServiceAuthError("expired", iss, kid) from None
    except jwt.InvalidAudienceError:
        raise ServiceAuthError("wrong_audience", iss, kid) from None
    except jwt.InvalidSignatureError:
        raise ServiceAuthError("bad_signature", iss, kid) from None
    except jwt.PyJWTError:
        raise ServiceAuthError("invalid_claims", iss, kid) from None
    try:
        sub, jti, scope = (
            _claim_str(claims, "sub"),
            _claim_str(claims, "jti"),
            _claim_str(claims, "scope"),
        )
        iat, exp = claims["iat"], claims["exp"]
        if sub != iss:
            raise ServiceAuthError("sub_mismatch")
        if isinstance(iat, bool) or isinstance(exp, bool) or not isinstance(iat, int | float):
            raise ServiceAuthError("invalid_times")
        if exp - iat > MAX_LIFETIME_S or exp <= iat:
            raise ServiceAuthError("lifetime_too_long")
        if not _JTI.match(jti):
            raise ServiceAuthError("invalid_jti")
    except ServiceAuthError as e:
        raise ServiceAuthError(e.reason, iss, kid) from None
    if required_scope not in scope.split() or required_scope not in scopes:
        raise ScopeDenied("scope_denied", iss, kid)
    _consume_jti(session_factory, client_pk, jti, datetime.fromtimestamp(exp, UTC), iss, kid)
    return ServiceContext(client_pk, iss, kid, scopes)


def _consume_jti(session_factory, client_pk, jti: str, expires_at: datetime, iss, kid) -> None:
    now = utcnow()
    with session_factory() as db:  # transaksion i veçantë: mbetet edhe nëse kërkesa dështon më vonë
        db.execute(delete(ServiceAssertionJti).where(ServiceAssertionJti.expires_at < now))
        db.add(ServiceAssertionJti(client_pk=client_pk, jti=jti,
                                   expires_at=expires_at + timedelta(seconds=LEEWAY_S)))  # fmt: skip
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ServiceAuthError("replayed", iss, kid) from None
