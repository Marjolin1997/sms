"""M10-S4: FASADA e vetme e autoritetit të sender-ave (`SMS_SENDER_AUTHORITY` = local | shadow | central). Asnjë degëzim autoriteti jashtë këtij moduli.

- **local** (parazgjedhje): thërret VETËM `sender_authorization` (S0) — sjellje, përjashtime dhe SQL identike me S3. Pa projeksion, pa Central.
- **shadow**: vendos autoriteti LOKAL (kthehet ai); paralelisht vlerësohet projeksioni i sinkronizuar dhe krahasimi regjistrohet (mospërputhjet gjithmonë, përputhjet të mostruara). Asnjë efekt te klienti.
- **central**: vendos projeksioni i sinkronizuar (`sms_synced_sender_*`) — vetëm lexime nga DB lokale, ASNJË thirrje rrjeti. Fail-static: projeksioni i vjetër NUK mohon; mungesa e provës (rresht mungon) mohon.
  Rishikimet lokale (approve/reject/revoke) bllokohen. `SenderId.status` s'preket kurrë nga ky modul.

Rregullat Central (V1, të vetmet): (1) politika efektive = e eksplicitja e sinkronizuar ose parazgjedhja virtuale `allowed=true, requires_approval=true`; (2) `allowed=false` ⇒ mohim;
(3) lejohet VETËM nëse ekziston autorizim i sinkronizuar `active` me status `approved` për (enterprise, shtet, çelës kanonik S0, lloj); (4) pending/rejected/revoked/mungon ⇒ mohim, edhe kur
`requires_approval=false` (miratimi automatik është vendim i S1 në Central — këtu s'shpikim miratim); (5) vjetërsia raportohet (`stale`), s'mohon kurrë."""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.scope import Owner
from app.core.timeutil import as_utc, utcnow
from app.models.enterprise_registry import lookup_id
from app.models.sender_authority import SenderAuthorityComparison
from app.models.sender_sync import (
    SenderSyncCursor,
    SyncedSenderAuthorization,
    SyncedSenderPolicy,
)
from app.services import sender_authorization as sa
from app.services.sender_authorization import InvalidSender, SenderNotAllowed

log = logging.getLogger("sms.sender.authority")

LOCAL, SHADOW, CENTRAL = "local", "shadow", "central"
FRESH_TTL_S = 30.0  # cache e freskisë në proces: ≤1 SELECT/30s për proces (jo për submit)
DENY_MESSAGE = "sender id is not approved for this account and country"


class SenderAuthorityFrozen(DomainError):
    code = "sender_authority_frozen"


def mode() -> str:
    return settings.sender_authority


def frozen() -> bool:
    return settings.sender_authority == CENTRAL


def require_local_review(action: str) -> None:
    """Thirret nga approve/reject/revoke lokal. Nën `central` Central është autoriteti i vetëm: shkrimi lokal do ta divergjonte gjendjen."""
    if frozen():
        raise SenderAuthorityFrozen(
            f"local sender review is frozen: SMS_SENDER_AUTHORITY=central ({action} is decided by Central)"
        )


# --- freskia (vetëm raportim) --------------------------------------------------------------------------------------------------------------------------

_fresh: dict = {"at": 0.0, "stale": True}


def clear_cache() -> None:
    _fresh["at"], _fresh["stale"] = 0.0, True


def projection_stale(db: Session, now: datetime | None = None) -> bool:
    """`True` nëse projeksioni s'është sinkronizuar kurrë ose suksesi i fundit është më i vjetër se `SMS_SENDER_PROJECTION_FRESH_SECONDS`. Informativ: NUK ndryshon vendimin."""
    t = time.monotonic()
    if now is None and t - _fresh["at"] < FRESH_TTL_S and _fresh["at"] > 0:
        return _fresh["stale"]
    last = db.scalar(select(SenderSyncCursor.last_success_at))
    ref_now = as_utc(now or utcnow())
    stale = (
        last is None
        or (ref_now - as_utc(last)).total_seconds() > settings.sender_projection_fresh_seconds
    )
    if now is None:
        _fresh["at"], _fresh["stale"] = t, stale
    return stale


# --- evaluatori Central --------------------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CentralEvaluation:
    allowed: bool
    reason: str  # approved | pending | rejected | revoked | missing | policy_denied | invalid_identity | no_enterprise
    country: str
    canonical_key: str
    sender_kind: str | None = None
    status: str | None = None
    registry_id: uuid.UUID | None = None
    external_ref: str | None = None
    decision_id: uuid.UUID | None = None
    policy_source: str | None = None
    policy_allowed: bool | None = None
    requires_approval: bool | None = None
    policy_revision: int | None = None
    cp_revision: int | None = None
    stale: bool = False


def enterprise_of(db: Session, owner: Owner) -> uuid.UUID | None:
    return owner.enterprise_id if isinstance(owner, TenantContext) else lookup_id(db, owner)


def _policy(db: Session, country: str, kind: str):
    p = db.scalar(
        select(SyncedSenderPolicy).where(
            SyncedSenderPolicy.country == country,
            SyncedSenderPolicy.sender_kind == kind,
            SyncedSenderPolicy.projection_state == "active",
        )
    )
    if p is None:
        return "default", True, True, None
    return "explicit", p.allowed, p.requires_approval, p.policy_revision


def evaluate_central_sender(
    db: Session, enterprise_id: uuid.UUID | None, country: str, value: str
) -> CentralEvaluation:
    """Vlerësim i pastër, vetëm-lexim mbi projeksionin (2 SELECT: politika + autorizimi). Identiteti = S0 (`sender_authorization.normalize`), pa normalizim të tretë."""
    country = country.upper()
    stale = projection_stale(db)
    try:
        n = sa.normalize(value)
    except InvalidSender:
        return CentralEvaluation(
            False,
            "invalid_identity",
            country,
            sa.canonical_key(country, sa.norm_of(value)),
            stale=stale,
        )
    key = sa.canonical_key(country, n.norm)
    if enterprise_id is None:
        return CentralEvaluation(False, "no_enterprise", country, key, n.kind.value, stale=stale)
    src, allowed, req, prev = _policy(db, country, n.kind.value)
    base = dict(country=country, canonical_key=key, sender_kind=n.kind.value, policy_source=src,
                policy_allowed=allowed, requires_approval=req, policy_revision=prev, stale=stale)  # fmt: skip
    if not allowed:
        return CentralEvaluation(False, "policy_denied", **base)
    rows = db.scalars(
        select(SyncedSenderAuthorization).where(
            SyncedSenderAuthorization.enterprise_id == enterprise_id,
            SyncedSenderAuthorization.country == country,
            SyncedSenderAuthorization.norm_value == n.norm,
            SyncedSenderAuthorization.sender_kind == n.kind.value,
            SyncedSenderAuthorization.projection_state == "active",
        )
    ).all()
    if not rows:
        return CentralEvaluation(False, "missing", **base)
    r = sorted(rows, key=lambda x: (x.status != "approved", x.id))[0]
    return CentralEvaluation(
        r.status == "approved", r.status, status=r.status, registry_id=r.registry_id,
        external_ref=r.external_ref, decision_id=r.decision_id, cp_revision=r.cp_revision,
        **{**base, "policy_revision": r.policy_revision if r.policy_revision is not None else prev},
    )  # fmt: skip


# --- krahasimi shadow ----------------------------------------------------------------------------------------------------------------------------------

_REASON_CATEGORY = {
    "missing": "central_missing", "pending": "central_pending", "rejected": "central_rejected",
    "revoked": "central_revoked", "policy_denied": "policy_mismatch",
    "invalid_identity": "sender_identity_mismatch",
}  # fmt: skip


def classify_comparison(local_allowed: bool, c: CentralEvaluation) -> str:
    if local_allowed == c.allowed:
        return "match_allowed" if local_allowed else "match_denied"
    if c.stale:
        return "projection_stale"  # mospërputhja mund të vijë nga vjetërsia; s'numërohet si drift kritik
    if not local_allowed:
        return "local_deny_central_allow"
    return _REASON_CATEGORY.get(c.reason, "local_allow_central_deny")


@dataclass(frozen=True, slots=True)
class Comparison:
    category: str
    local_allowed: bool
    central: CentralEvaluation
    local_sender_ref: int | None
    enterprise_id: uuid.UUID | None

    @property
    def mismatch(self) -> bool:
        return not self.category.startswith("match_")


def _sampled(ref: str) -> bool:
    pct = settings.sender_shadow_sample_pct
    return int.from_bytes(hashlib.sha256(ref.encode()).digest()[:4], "big") % 100 < pct


def comparison_row(cmp: Comparison, ref: str) -> SenderAuthorityComparison:
    c = cmp.central
    return SenderAuthorityComparison(
        ref=ref[:64], enterprise_id=cmp.enterprise_id, country=c.country, sender_kind=c.sender_kind,
        category=cmp.category, local_allowed=cmp.local_allowed, central_allowed=c.allowed,
        central_reason=c.reason, local_sender_ref=cmp.local_sender_ref, central_registry_ref=c.registry_id,
        central_policy_revision=c.policy_revision, central_cp_revision=c.cp_revision,
        identity_hash=hashlib.sha256(c.canonical_key.encode()).hexdigest()[:16], projection_stale=c.stale,
    )  # fmt: skip


def record(db: Session, cmp: Comparison | None, ref: str) -> None:
    """Brenda tx të thirrësit (mesazhi ekziston). Mospërputhjet gjithmonë; përputhjet mostra deterministike."""
    if cmp is not None and (cmp.mismatch or _sampled(ref)):
        db.add(comparison_row(cmp, ref))


def _record_detached(db: Session, cmp: Comparison) -> None:
    """Rruga e mohimit lokal (s'ka mesazh, tx i thirrësit rikthehet): sesion i shkurtër i veçantë. Dështimi s'prek kurrë mohimin."""
    try:
        with Session(bind=db.get_bind()) as s:
            s.add(comparison_row(cmp, f"denied-{uuid.uuid4().hex}"))
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception("sender shadow comparison could not be persisted")


# --- fasada --------------------------------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    source: str  # local | central
    allowed: bool
    local: sa.Authorization | None = None
    central: CentralEvaluation | None = None
    comparison: Comparison | None = None

    def message_fields(self) -> dict:
        """Provenanca e ngrirë në `Message`. `central`: ref/vendim lokal mbeten NULL (s'përzihen)."""
        if self.source == CENTRAL:
            c = self.central
            return dict(
                sender_authority_source=CENTRAL, sender_registry_ref=c.registry_id,
                sender_central_decision_ref=c.decision_id, sender_central_revision=c.cp_revision,
                sender_policy_revision=c.policy_revision,
            )  # fmt: skip
        a = self.local
        return dict(
            sender_authority_source=LOCAL, sender_ref=a.sender_ref, sender_decision_ref=a.decision_ref,
            sender_policy_revision=a.policy_revision,
        )  # fmt: skip

    def record_comparison(self, db: Session, ref: str) -> None:
        record(db, self.comparison, ref)


def check_outbound_authority(
    db: Session, owner: Owner, country: str, value: str
) -> AuthorityDecision:
    """Vendimi i dërgimit sipas autoritetit aktiv. Nuk ngre për mohim — shih `assert_outbound_authority`."""
    m = mode()
    if m == LOCAL:
        a = sa.check_outbound(db, owner, country, value)
        return AuthorityDecision(LOCAL, a.allowed, local=a)
    eid = enterprise_of(db, owner)
    c = evaluate_central_sender(db, eid, country, value)
    if m == CENTRAL:
        return AuthorityDecision(CENTRAL, c.allowed, central=c)
    a = sa.check_outbound(db, owner, country, value)
    cmp = Comparison(classify_comparison(a.allowed, c), a.allowed, c, a.sender_ref, eid)
    return AuthorityDecision(LOCAL, a.allowed, local=a, central=c, comparison=cmp)


def assert_outbound_authority(
    db: Session, owner: Owner, country: str, value: str
) -> AuthorityDecision:
    if mode() == LOCAL:  # rruga e sotme, e pandryshuar (e njëjta SQL, i njëjti përjashtim)
        a = sa.assert_outbound(db, owner, country, value)
        return AuthorityDecision(LOCAL, True, local=a)
    d = check_outbound_authority(db, owner, country, value)
    if not d.allowed:
        if d.comparison is not None and d.comparison.mismatch:
            _record_detached(
                db, d.comparison
            )  # local_deny_central_allow dhe të ngjashme: provë edhe pa mesazh
        raise SenderNotAllowed(DENY_MESSAGE)
    return d


def has_approved_sender_authority(db: Session, owner: Owner, value: str) -> bool:
    """Prekontrolli i fushatës (pa shtet): i njëjti autoritet si submit, version i trashë (≥1 shtet ku lejohet). Vendimi përfundimtar mbetet per-marrës në `submit`."""
    if mode() != CENTRAL:
        return sa.has_approved_sender(db, owner, value)
    eid = enterprise_of(db, owner)
    if eid is None:
        return False
    try:
        n = sa.normalize(value)
    except InvalidSender:
        return False
    rows = db.scalars(
        select(SyncedSenderAuthorization).where(
            SyncedSenderAuthorization.enterprise_id == eid,
            SyncedSenderAuthorization.norm_value == n.norm,
            SyncedSenderAuthorization.sender_kind == n.kind.value,
            SyncedSenderAuthorization.status == "approved",
            SyncedSenderAuthorization.projection_state == "active",
        )
    ).all()
    return any(_policy(db, r.country, r.sender_kind)[1] for r in rows)


# --- rikontrolli para dispatch-it (VETËM themeli; i palidhur me process_one NUK është aktivizuar) ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchCheck:
    block: bool
    reason: str
    source: str  # local | central | none
    stale: bool = False


def recheck_for_dispatch(db: Session, m) -> DispatchCheck:
    """Gjendja AKTUALE e burimit që e autorizoi mesazhin `m` (vetëm lexim lokal, pa rrjet). Semantika e propozuar:
    - bllokon: `revoked`/`rejected`/`pending` eksplicit (Central: nga projeksioni; lokal: S0) dhe politika Central `allowed=false`;
    - NUK bllokon: projeksion i vjetër (stale), rresht projeksioni që mungon/tërhequr (boshllëk/ndërprerje: s'shpikim revokim), mesazh pa provenancë (para M10).
    Nuk thirret nga `process_one` (aktivizimi është vendim i veçantë)."""
    src = getattr(m, "sender_authority_source", None)
    if src == CENTRAL:
        ref = m.sender_registry_ref
        if ref is None:
            return DispatchCheck(False, "no_registry_ref", CENTRAL)
        row = db.scalar(
            select(SyncedSenderAuthorization).where(SyncedSenderAuthorization.registry_id == ref)
        )
        stale = projection_stale(db)
        if row is None or row.projection_state != "active":
            return DispatchCheck(False, "projection_missing", CENTRAL, stale)
        if row.status != "approved":
            return DispatchCheck(True, row.status, CENTRAL, stale)
        if not _policy(db, row.country, row.sender_kind)[1]:
            return DispatchCheck(True, "policy_denied", CENTRAL, stale)
        return DispatchCheck(False, "approved", CENTRAL, stale)
    a = sa.recheck_for_dispatch(db, m.sender_ref)
    if a is None:
        return DispatchCheck(False, "no_provenance", "none")
    return DispatchCheck(not a.allowed, a.category, LOCAL)
