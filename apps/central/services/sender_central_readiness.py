"""M10-S1: gatishmëria LOKALE e Central për autoritetin e sender-ave (jo `sender_policy_readiness` final — ajo vjen pas sync-ut). VETËM LEXIM, pa PII.

Kontrollon: revizionet e politikës të njëpasnjëshme dhe me `effective_from` rritës · regjistri i vlefshëm (approved ⇔ approved_key = shtet:norm) ·
çdo rresht ka vendimin aktual dhe përputhet me gjendjen · zinxhiri i vendimeve fillon me `requested` dhe ndjek makinën e gjendjeve me `seq` të plotë ·
çdo i miratuar ka provenancë politike · asnjë i miratuar nën politikë `allowed=false` · çdo sender ka audit · unikaliteti i `approved_key`."""

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.models.audit import AuditLog
from apps.central.models.sender import CountrySenderPolicy, SenderDecision, SenderRegistry
from apps.central.services import sender_identity as ident
from apps.central.services import senders

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_TRANSITIONS = {
    ("requested", None): "pending",
    ("approved", "pending"): "approved",
    ("rejected", "pending"): "rejected",
    ("revoked", "approved"): "revoked",
    ("resubmitted", "rejected"): "pending",
    ("resubmitted", "revoked"): "pending",
}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _c(name, bad, ok, bad_text, level=FAIL):
    return Check(name, level if bad else PASS, bad_text if bad else ok)


def checks(db: Session) -> list[Check]:
    out: list[Check] = []
    scopes: dict = defaultdict(list)
    for p in db.scalars(
        select(CountrySenderPolicy).order_by(
            CountrySenderPolicy.country,
            CountrySenderPolicy.sender_kind,
            CountrySenderPolicy.revision,
        )
    ):
        scopes[(p.country, p.sender_kind)].append(p)
    bad_scopes = [
        f"{c}/{k}"
        for (c, k), rows in scopes.items()
        if [r.revision for r in rows] != list(range(1, len(rows) + 1))
        or any(rows[i].effective_from >= rows[i + 1].effective_from for i in range(len(rows) - 1))
    ]
    out.append(
        _c(
            "policy_revisions_coherent",
            bad_scopes,
            f"{len(scopes)} scope(s), revisions contiguous and strictly increasing",
            f"incoherent policy revisions: {bad_scopes[:5]}",
        )
    )

    reg = list(db.scalars(select(SenderRegistry)))
    bad_reg = [
        str(r.id)
        for r in reg
        if (r.current_status == "approved") != (r.approved_key is not None)
        or (
            r.approved_key is not None
            and r.approved_key != ident.canonical_key(r.country, r.norm_value)
        )
    ]
    out.append(
        _c(
            "registry_states_valid",
            bad_reg,
            f"{len(reg)} sender(s)",
            f"{len(bad_reg)} sender(s) with an invalid approved_key/status",
        )
    )
    dup = db.execute(
        select(SenderRegistry.approved_key)
        .where(SenderRegistry.approved_key.is_not(None))
        .group_by(SenderRegistry.approved_key)
        .having(func.count() > 1)
    ).all()
    out.append(_c("approved_key_unique", dup, "unique", f"{len(dup)} duplicated approved_key"))

    decs: dict = defaultdict(list)
    for d in db.scalars(
        select(SenderDecision).order_by(SenderDecision.registry_id, SenderDecision.seq)
    ):
        decs[d.registry_id].append(d)
    no_cur, mismatch, chain_bad, no_prov = [], [], [], []
    for r in reg:
        ds = decs.get(r.id, [])
        if not ds:
            no_cur.append(str(r.id))
            continue
        last = ds[-1]
        if r.current_decision_id != last.id or last.to_status != r.current_status:
            mismatch.append(str(r.id))
        prev = None
        for i, d in enumerate(ds, start=1):
            if (
                d.seq != i
                or _TRANSITIONS.get((d.decision, prev)) != d.to_status
                or d.from_status != prev
            ):
                chain_bad.append(str(r.id))
                break
            prev = d.to_status
        if r.current_status == "approved" and last.policy_source not in ("explicit", "default"):
            no_prov.append(str(r.id))
    out.append(
        _c(
            "registry_has_decision_history",
            no_cur,
            "every sender has history",
            f"{len(no_cur)} sender(s) without decisions",
        )
    )
    out.append(
        _c(
            "current_decision_matches_state",
            mismatch,
            "consistent",
            f"{len(mismatch)} sender(s) whose current decision/status disagree",
        )
    )
    out.append(
        _c(
            "decision_chain_valid",
            chain_bad,
            "chains follow the state machine",
            f"{len(chain_bad)} sender(s) with an invalid decision chain",
        )
    )
    out.append(
        _c(
            "approved_has_policy_provenance",
            no_prov,
            "ok",
            f"{len(no_prov)} approved sender(s) without policy provenance",
        )
    )

    impossible = []
    for r in reg:
        if r.current_status == "approved":
            v = senders.effective_policy(db, r.country, r.sender_kind)
            if not v.allowed:
                impossible.append(str(r.id))
    out.append(
        _c(
            "no_approved_under_disallowing_policy",
            impossible,
            "none",
            f"{len(impossible)} approved sender(s) under an allowed=false policy (needs system revocation)",
        )
    )

    audited = {
        x
        for x in db.scalars(select(AuditLog.resource_id).where(AuditLog.resource_type == "sender"))
    }
    missing = [str(r.id) for r in reg if str(r.id) not in audited]
    out.append(
        _c(
            "senders_have_audit",
            missing,
            "audited",
            f"{len(missing)} sender(s) without any audit row",
        )
    )
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )
