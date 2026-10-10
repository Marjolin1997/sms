"""M10-S4: bootstrap i senderave ekzistues të Enterprise drejt regjistrit Central (artefakti `sender-bootstrap.v1`). Klasifikim DRY-RUN si parazgjedhje; zbatim i bashkuar me hash-in e miratuar.

Parimet:
- **Pa konvertim të verbër:** miratimi historik lokal importohet VETËM kur është i sigurt: politika Central e lejon, s'ka konflikt identiteti, s'ka konflikt çelësi global të miratuar. Çdo gjë tjetër raportohet (pa shkrim).
- **Rruga e dedikuar** (jo rrjedha normale e rishikimit): regjistri krijohet me `source='import'`, aktori i sistemit `system:sender-bootstrap`, evidenca `bootstrap:<run>`; auditi i S1 shkruhet si zakonisht.
- **Idempotent/rifillueshëm:** identiteti (enterprise, external_ref) është çelësi; rikalimi riklasifikon dhe kapërcen ç'është bërë. Transaksion për batch (jo një tx për të gjithë), i kufizuar sipas enterprise-it opsionalisht.
- Kategoritë (raport makinëlexueshëm): exact_match · missing_in_central · local_pending · local_inactive · local_approved_central_{pending|rejected|revoked} · identity_conflict · global_key_conflict · policy_denied ·
  invalid_legacy_identity · missing_enterprise_mapping. `UNRESOLVED` bllokojnë gatishmërinë."""

import hashlib
import json
import uuid
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.sender import SenderBootstrapRun, SenderRegistry
from apps.central.services import audit, senders
from apps.central.services import sender_identity as ident

SCHEMA = "sender-bootstrap.v1"
REPORT_SCHEMA = "sender-bootstrap-report.v1"
VERSION = 1
ACTOR = "system:sender-bootstrap"
UNRESOLVED = frozenset({
    "identity_conflict", "global_key_conflict", "policy_denied", "invalid_legacy_identity",
    "missing_enterprise_mapping", "local_approved_central_pending", "local_approved_central_rejected",
    "local_approved_central_revoked",
})  # fmt: skip
ACTIONS = {"missing_in_central": "import_approved", "local_pending": "import_pending"}
_FIELDS = (
    "sender_id",
    "external_ref",
    "enterprise_id",
    "country",
    "display_value",
    "kind",
    "status",
)
MAX_ITEMS = 200_000


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def validate(art: object) -> list[dict]:
    if (
        not isinstance(art, dict)
        or art.get("schema") != SCHEMA
        or not isinstance(art.get("senders"), list)
    ):
        raise Invalid(f"artifact must be an object with schema {SCHEMA} and a senders array")
    if len(art["senders"]) > MAX_ITEMS:
        raise Invalid("artifact is too large")
    items = []
    for i, it in enumerate(art["senders"]):
        if not isinstance(it, dict) or set(it) != set(_FIELDS):
            raise Invalid(f"senders[{i}] must have exactly the fields {list(_FIELDS)}")
        if isinstance(it["sender_id"], bool) or not isinstance(it["sender_id"], int):
            raise Invalid(f"senders[{i}].sender_id must be an integer")
        if it["status"] not in ("pending", "approved", "rejected", "revoked"):
            raise Invalid(f"senders[{i}].status is invalid")
        items.append(it)
    ids = [it["sender_id"] for it in items]
    if len(set(ids)) != len(ids):
        raise Invalid("duplicate sender_id in the artifact")
    return sorted(items, key=lambda x: (str(x["enterprise_id"]), x["sender_id"]))


def artifact_hash(art: dict) -> str:
    return hashlib.sha256(canonical(validate(art))).hexdigest()


def _registry(db: Session, eid, **kw):
    return db.scalar(select(SenderRegistry).where(SenderRegistry.enterprise_id == eid, *[getattr(SenderRegistry, k) == v for k, v in kw.items()]))  # fmt: skip


def classify(db: Session, it: dict, now: datetime) -> str:
    try:
        eid = uuid.UUID(str(it["enterprise_id"])) if it["enterprise_id"] else None
    except ValueError:
        eid = None
    if eid is None or db.get(Enterprise, eid) is None:
        return "missing_enterprise_mapping"
    try:
        i = ident.identity(it["country"], it["display_value"])
    except Invalid:
        return "invalid_legacy_identity"
    if it["kind"] != i.kind:
        return "invalid_legacy_identity"
    reg = _registry(db, eid, external_ref=it["external_ref"])
    if reg is not None:
        if (reg.country, reg.sender_kind, reg.display_value) != (i.country, i.kind, i.display):
            return "identity_conflict"
        if it["status"] == "approved" and reg.current_status != "approved":
            return f"local_approved_central_{reg.current_status}"
        return "exact_match"
    if _registry(db, eid, country=i.country, norm_value=i.norm) is not None:
        return "identity_conflict"  # i njëjti sender nën external_ref tjetër
    if it["status"] == "approved":
        if not senders.effective_policy(db, i.country, i.kind, now).allowed:
            return "policy_denied"
        if (
            db.scalar(
                select(SenderRegistry.id).where(SenderRegistry.approved_key == i.approved_key)
            )
            is not None
        ):
            return "global_key_conflict"
        return "missing_in_central"
    return "local_pending" if it["status"] == "pending" else "local_inactive"


def _apply(db: Session, it: dict, category: str, evidence: str, now: datetime) -> str:
    try:
        with db.begin_nested():
            res = senders.request_sender(
                db, ACTOR, uuid.UUID(it["enterprise_id"]), it["external_ref"], it["country"],
                it["display_value"], evidence, source="import", now=now,
            )  # fmt: skip
            row = res.sender
            if category == "missing_in_central":
                if row.current_status == "pending":
                    senders.approve(db, ACTOR, row.id, evidence, now=now)
                    row = db.get(SenderRegistry, row.id)
                if row.current_status != "approved":
                    raise Conflict("sender could not be approved by the import")
                return "imported_approved"
            return "imported_pending"
    except (Conflict, Invalid) as e:
        return f"apply_conflict:{type(e).__name__}"


def run(
    db: Session,
    art: dict,
    *,
    apply: bool = False,
    actor_id: uuid.UUID | None = None,
    batch_size: int = 100,
    enterprise_id: uuid.UUID | None = None,
    source_revision: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Dry-run: pa asnjë shkrim. Apply: commit për batch (`batch_size`), rresht `SenderBootstrapRun` + audit; i përsëritshëm."""
    if batch_size < 1 or batch_size > 5000:
        raise Invalid("batch_size must be 1..5000")
    items = validate(art)
    ahash = artifact_hash(art)
    now = now or utcnow()
    if enterprise_id is not None:
        items = [x for x in items if x["enterprise_id"] == str(enterprise_id)]
    run_row = None
    evidence = "bootstrap:dry-run"
    if apply:
        run_row = SenderBootstrapRun(
            artifact_hash=ahash, source_revision=source_revision, actor_id=actor_id, started_at=now,
            sender_count=len(items), tenant_count=len({x["enterprise_id"] for x in items}),
            imported_count=0, unresolved_count=0, status="running",
        )  # fmt: skip
        db.add(run_row)
        db.commit()
        evidence = f"bootstrap:{run_row.id.hex[:12]}"
    out, pending = [], 0
    for it in items:
        cat = classify(db, it, now)
        action = ACTIONS.get(cat)
        result = None
        if apply and action:
            result = _apply(db, it, cat, evidence, now)
            pending += 1
            if pending % batch_size == 0:
                db.commit()
        out.append({"sender_id": it["sender_id"], "external_ref": it["external_ref"], "enterprise_id": it["enterprise_id"], "category": cat, "action": action, "result": result})  # fmt: skip
    summary = Counter(r["category"] for r in out)
    unresolved = sum(
        1
        for r in out
        if r["category"] in UNRESOLVED or (r["result"] or "").startswith("apply_conflict")
    )
    imported = sum(1 for r in out if (r["result"] or "").startswith("imported"))
    report = {
        "schema": REPORT_SCHEMA, "bootstrap_version": VERSION, "mode": "apply" if apply else "dry_run",
        "artifact_hash": ahash, "summary": dict(sorted(summary.items())), "unresolved": unresolved,
        "imported": imported, "senders": len(out), "tenants": len({r["enterprise_id"] for r in out}), "items": out,
    }  # fmt: skip
    report["report_hash"] = hashlib.sha256(canonical({k: report[k] for k in ("artifact_hash", "mode", "summary", "items")})).hexdigest()  # fmt: skip
    if apply:
        run_row.status, run_row.completed_at = "completed", utcnow()
        run_row.report_hash, run_row.imported_count, run_row.unresolved_count = (
            report["report_hash"],
            imported,
            unresolved,
        )
        audit.record_system(
            db, label=ACTOR, action="sender.bootstrap", resource_type="sender_bootstrap", resource_id=run_row.id,
            detail={"artifact_hash": ahash, "report_hash": report["report_hash"], "imported": imported, "unresolved": unresolved, "senders": len(out)},
            now=utcnow(),
        )  # fmt: skip
        db.commit()
        report["run_id"] = str(run_row.id)
    return report
