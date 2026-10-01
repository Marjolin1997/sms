"""Consent: opt-in/opt-out me prova, kontroll para dërgimit, fjalët STOP/START."""

import hashlib
import hmac
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import Conflict, DomainError
from app.core.scope import Owner, owned, ref
from app.models.contacts import ConsentAction, ConsentEvent, ConsentState
from app.services import events

CHANNELS = {"sms", "email"}
CATEGORIES = {"transactional", "marketing"}
# Arsyet e opt-out. HARD bllokon çdo kategori; SOFT vetëm marketing.
HARD_REASONS = {"stop_keyword", "bounce_hard", "complaint", "invalid", "erasure"}
SOFT_REASONS = {"unsubscribe", "manual"}
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,63}$")
_PHONE = re.compile(r"^[1-9]\d{6,14}$")

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "ndalo"}
START_WORDS = {"start", "unstop", "yes", "fillo"}


class InvalidAddress(DomainError):
    code = "invalid_address"


class RecipientSuppressed(DomainError):
    code = "recipient_suppressed"


def normalize(channel: str, address: str) -> str:
    if channel not in CHANNELS:
        raise InvalidAddress("channel must be 'sms' or 'email'")
    a = address.strip()
    if channel == "sms":
        a = a.lstrip("+")
        if not _PHONE.match(a):
            raise InvalidAddress("phone must be E.164")
        return a
    a = a.lower()
    if len(a) > 254 or not _EMAIL.match(a):
        raise InvalidAddress("invalid email address")
    return a


def address_hash(owner_ref: str, channel: str, normalized: str) -> str:
    if not settings.pii_hmac_key:
        raise RuntimeError("SMS_PII_HMAC_KEY is not configured")  # fail closed
    msg = f"{owner_ref}\x00{channel}\x00{normalized}".encode()
    return hmac.new(settings.pii_hmac_key.encode(), msg, hashlib.sha256).hexdigest()


def _state(db: Session, owner: Owner, channel: str, h: str, lock: bool = False):
    q = select(ConsentState).where(
        owned(ConsentState, owner),
        ConsentState.channel == channel,
        ConsentState.address_hash == h,
    )
    return db.scalar(q.with_for_update() if lock else q)


def _record(
    db: Session,
    owner: Owner,
    channel: str,
    address: str,
    action: str,
    reason: str,
    source: str,
    actor: str,
    evidence: str | None = None,
) -> ConsentState:
    """Shkruan provën (event) dhe përditëson gjendjen aktuale në të njëjtin transaksion."""
    norm = normalize(channel, address)
    h = address_hash(ref(owner), channel, norm)
    act = ConsentAction(action)
    if act == ConsentAction.OPT_OUT and reason not in HARD_REASONS | SOFT_REASONS:
        raise Conflict(f"unknown opt-out reason '{reason}'")
    if act == ConsentAction.OPT_IN:
        reason = "opt_in"
        if not evidence or len(evidence.strip()) < 3:
            raise Conflict("opt-in requires evidence (how and when the person consented)")
    if not source or not actor:
        raise Conflict("source and actor are required")

    st = _state(db, owner, channel, h, lock=True)
    if act == ConsentAction.OPT_IN and st and not st.opted_in and st.hard:
        if st.reason != "stop_keyword":  # vetëm një STOP i vetë personit mund të zhbëhet
            raise Conflict(f"address is blocked ({st.reason}) and cannot be re-subscribed")

    ev = ConsentEvent(
        owner_ref=ref(owner), channel=channel, address_hash=h, action=act,
        reason=reason, source=source, evidence=evidence, actor=actor,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    opted_in = act == ConsentAction.OPT_IN
    hard = (not opted_in) and (reason in HARD_REASONS)
    if st is None:
        try:
            with db.begin_nested():
                st = ConsentState(
                    owner_ref=ref(owner), channel=channel, address_hash=h, opted_in=opted_in,
                    hard=hard, reason=reason, last_event_id=ev.id,
                )  # fmt: skip
                db.add(st)
                db.flush()
            return st
        except IntegrityError:  # garë: dikush e krijoi njëkohësisht
            st = _state(db, owner, channel, h, lock=True)
    # një bounce/complaint/erasure i ri nuk zbutet nga një opt-out "soft" i mëvonshëm
    if not opted_in and st.hard and not hard:
        hard, reason = True, st.reason
    st.opted_in, st.hard, st.reason, st.last_event_id = opted_in, hard, reason, ev.id
    db.flush()
    return st


def record(
    db: Session,
    owner: Owner,
    channel: str,
    address: str,
    action: str,
    reason: str,
    source: str,
    actor: str,
    evidence: str | None = None,
) -> ConsentState:
    st = _record(db, owner, channel, address, action, reason, source, actor, evidence)
    kind = "opted_in" if action == "opt_in" else "opted_out"
    # adresa futet qëllimisht: klienti e përdor për të sinkronizuar CRM-në (retention e eventeve)
    events.emit(
        db, owner, f"consent.{kind}", "consent", st.id,
        {"channel": channel, "address": normalize(channel, address), "reason": st.reason,
         "hard": st.hard},
    )  # fmt: skip
    return st


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str  # ok | no_consent | opted_out | blocked:<arsyeja>


def decide(state: ConsentState | None, category: str) -> Decision:
    if category not in CATEGORIES:
        raise Conflict("category must be 'transactional' or 'marketing'")
    if state is not None and not state.opted_in and state.hard:
        return Decision(False, f"blocked:{state.reason}")
    if category == "transactional":
        return Decision(True, "ok")
    if state is None:
        return Decision(False, "no_consent")  # marketing kërkon opt-in të provuar
    return Decision(True, "ok") if state.opted_in else Decision(False, "opted_out")


def check(db: Session, owner: Owner, channel: str, address: str, category: str) -> Decision:
    norm = normalize(channel, address)
    st = _state(db, owner, channel, address_hash(ref(owner), channel, norm))
    return decide(st, category)


def assert_may_send(db: Session, owner: Owner, channel: str, address: str, category: str):
    d = check(db, owner, channel, address, category)
    if not d.allowed:
        raise RecipientSuppressed(f"recipient cannot be contacted: {d.reason}")


def apply_inbound_keyword(db: Session, owner: Owner, from_number: str, text: str) -> str | None:
    """SMS hyrës: STOP → bllokim i plotë; START → rikthim (vetëm pas STOP të vetë personit)."""
    word = re.sub(r"[^\w]", "", text.strip().lower())
    if word in STOP_WORDS:
        record(db, owner, "sms", from_number, "opt_out", "stop_keyword", "inbound_sms",
               "inbound", evidence=text[:200])  # fmt: skip
        return "opt_out"
    if word in START_WORDS:
        try:
            record(db, owner, "sms", from_number, "opt_in", "opt_in", "inbound_sms",
                   "inbound", evidence=f"inbound keyword: {text[:100]}")  # fmt: skip
        except Conflict:
            return None
        return "opt_in"
    return None
