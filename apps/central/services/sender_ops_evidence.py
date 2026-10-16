"""M11-D2: prova operacionale e Enterprise brenda pamjes operacionale të Central. VETËM LEXIM nga snapshot-i i fundit i ruajtur (`sender_ops_snapshots`); pa thirrje drejt Enterprise.

Rregulla (server-side; frontend-i s'llogarit asgjë):
- Pragjet e alarmeve vijnë nga Enterprise (nivelet `ok|warning|critical` të `sender_alerts` brenda snapshot-it): Central s'shpik pragje të reja për to.
- `null` / seksion mungon = `unknown` (kurrë "zero e shëndetshme"); përjashtim i dokumentuar: `oldest_open_age_seconds: null` = s'ka kërkesa të hapura ⇒ pass.
- Freskia: mosha nga `received_at`. ≤ stale ⇒ e freskët · ≤ 6×stale ⇒ `warn` · më shumë ⇒ `fail`. Kur snapshot-i s'është i freskët, çdo `pass` i prejardhur nga Enterprise
  degradohet në `unknown` (s'mund të konfirmohet); `warn/fail` ruhen (lajmi i keq nuk fshihet).
- Pa snapshot / skemë e pambështetur / payload i dëmtuar ⇒ `unknown`, kurrë 500."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.models.sender import SenderOpsSnapshot
from apps.central.models.service_auth import ServiceClient
from packages.contracts.control_plane.sender import ops_v1 as v1

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
EXPIRE_FACTOR = 6
KNOWN_BOOTSTRAP = frozenset({
    "identity_conflict", "global_key_conflict", "policy_denied", "invalid_legacy_identity", "missing_enterprise_mapping",
    "local_approved_central_pending", "local_approved_central_rejected", "local_approved_central_revoked", "missing_in_central",
})  # fmt: skip
# alarm i Enterprise → (seksioni, kodi i kontrollit)
ALERT_CHECKS = {
    "sender_sync_lag": ("sync", "enterprise_sync_lag"),
    "sender_sync_age_seconds": ("sync", "enterprise_sync_age"),
    "sender_sync_gap_recoveries_total": ("sync", "enterprise_sync_gap_recoveries"),
    "sender_projection_stale": ("sync", "enterprise_projection_stale"),
    "sender_request_oldest_age_seconds": ("requests", "enterprise_request_age"),
    "sender_request_permanent_failures": ("requests", "enterprise_request_failures"),
    "sender_bootstrap_unresolved": ("bootstrap", "enterprise_bootstrap_unresolved"),
    "sender_shadow_critical_drift": ("enterprise_evidence", "shadow_critical_drift"),
    "sender_central_deny_ratio": ("enterprise_evidence", "central_deny_ratio"),
    "sender_dispatch_recheck_blocked_1h": ("enterprise_evidence", "dispatch_recheck_blocked"),
    "sender_policy_readiness_fail": ("enterprise_evidence", "policy_readiness"),
}  # fmt: skip
_NONE_IS_OK = frozenset({"sender_request_oldest_age_seconds"})
_LEVEL = {"ok": PASS, "warning": WARN, "critical": FAIL}


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d: datetime | None) -> str | None:
    return None if d is None else _aware(d).isoformat()


def _chk(code: str, status: str, summary: str, **details) -> dict:
    flat = {
        k: v for k, v in details.items() if v is not None and isinstance(v, (str, int, float, bool))
    }
    return {
        "code": code,
        "status": status,
        "summary": summary,
        "details": flat,
        "source": "enterprise",
    }


def _num(v):
    return None if v is None else (round(v, 4) if isinstance(v, float) else v)


def latest(db: Session) -> tuple[SenderOpsSnapshot | None, int, str | None]:
    row = db.scalars(
        select(SenderOpsSnapshot).order_by(SenderOpsSnapshot.received_at.desc()).limit(1)
    ).first()
    n = db.scalar(select(func.count()).select_from(SenderOpsSnapshot)) or 0
    who = (
        None
        if row is None
        else db.scalar(select(ServiceClient.client_id).where(ServiceClient.id == row.client_pk))
    )
    return row, int(n), (who[:24] if who else None)


def freshness(row: SenderOpsSnapshot, now: datetime) -> tuple[str, int]:
    age = max(0, int((_aware(now) - _aware(row.received_at)).total_seconds()))
    limit = settings.sender_ops_stale_seconds
    return (
        "fresh" if age <= limit else "stale" if age <= limit * EXPIRE_FACTOR else "expired"
    ), age


def _alert_checks(data: dict) -> list[tuple[str, dict]]:
    a = data.get("alerts")
    out = []
    for item in (a or {}).get("items", []):
        target = ALERT_CHECKS.get(item["name"])
        if target is None:
            continue
        section, code = target
        st = _LEVEL[item["level"]]
        if st == PASS and item["value"] is None and item["name"] not in _NONE_IS_OK:
            st = UNKNOWN
        out.append(
            (
                section,
                _chk(
                    code,
                    st,
                    f"{item['name']}: value={_num(item['value'])}, threshold={_num(item['threshold'])}",
                    value=_num(item["value"]),
                    threshold=_num(item["threshold"]),
                    level=item["level"],
                ),
            )
        )
    return out


def _sync_runtime(s: dict | None) -> dict:
    if s is None:
        return _chk(
            "enterprise_sync_runtime", UNKNOWN, "Sync evidence was unavailable in the snapshot."
        )
    if not s["enabled"]:
        return _chk(
            "enterprise_sync_runtime",
            UNKNOWN,
            "Sender sync is not enabled on Enterprise (not configured).",
            enabled=False,
        )
    if not s["initialized"]:
        return _chk(
            "enterprise_sync_runtime",
            WARN,
            "The Enterprise sync cursor is not initialized yet.",
            enabled=True,
            initialized=False,
        )
    keys = (
        "cursor_seq",
        "latest_central_seq",
        "lag",
        "last_success_age_seconds",
        "snapshot_age_seconds",
        "failure_count",
        "gap_recoveries",
        "policy_count",
        "authorization_count",
        "withdrawn_count",
    )
    lag = s["lag"]
    return _chk(
        "enterprise_sync_runtime",
        PASS,
        f"Enterprise cursor {s['cursor_seq']} of Central {s['latest_central_seq']}"
        + ("" if lag is None else f" (lag {lag})")
        + ".",
        **{k: s[k] for k in keys},
    )


def _request_outbox(r: dict | None) -> dict:
    if r is None:
        return _chk(
            "enterprise_request_outbox",
            UNKNOWN,
            "Request reporter evidence was unavailable in the snapshot.",
        )
    keys = (
        "pending",
        "retry",
        "sending",
        "failed",
        "sent",
        "stuck_sending",
        "oldest_open_age_seconds",
        "last_sent_age_seconds",
        "attempts_max",
    )
    d = {k: r[k] for k in keys}
    flat = {f"failed_{k}": v for k, v in r["failed_by_category"].items()}
    if not r["enabled"] and not any(
        r[k] for k in ("pending", "retry", "sending", "failed", "sent")
    ):
        return _chk(
            "enterprise_request_outbox",
            UNKNOWN,
            "Request reporting is not enabled on Enterprise (not configured).",
            enabled=False,
        )
    status = FAIL if r["failed"] else WARN if (r["retry"] or r["stuck_sending"]) else PASS
    return _chk(
        "enterprise_request_outbox",
        status,
        f"pending {r['pending']}, retry {r['retry']}, sending {r['sending']}, failed {r['failed']}.",
        **d,
        **flat,
    )


def _bootstrap_categories(b: dict | None) -> dict:
    if b is None:
        return _chk(
            "bootstrap_issue_categories",
            UNKNOWN,
            "Bootstrap evidence was unavailable in the snapshot.",
        )
    if b["state"] is None:
        return _chk(
            "bootstrap_issue_categories", UNKNOWN, "No bootstrap is recorded on Enterprise."
        )
    cats: dict[str, int] = {}
    for k, n in b["issues"]["open_by_category"].items():
        key = k if k in KNOWN_BOOTSTRAP else "other"
        cats[key] = cats.get(key, 0) + n
    opened = b["issues"]["open"]
    return _chk(
        "bootstrap_issue_categories",
        WARN if opened else PASS,
        f"{opened} open and {b['issues']['resolved']} resolved bootstrap issue(s).",
        open=opened,
        resolved=b["issues"]["resolved"],
        **{f"open_{k}": v for k, v in sorted(cats.items())},
    )


def _agreement(b: dict | None, central_run: dict | None) -> dict:
    code = "bootstrap_evidence_agreement"
    if b is None:
        return _chk(code, UNKNOWN, "Enterprise bootstrap evidence was unavailable in the snapshot.")
    e = b["state"]
    c_done = bool(central_run and central_run.get("status") == "completed")
    e_done = bool(e and e["completed"])
    if not c_done and not e_done:
        return _chk(code, UNKNOWN, "No completed bootstrap on either side yet.")
    if c_done != e_done or (
        c_done
        and e_done
        and (central_run["sender_count"], central_run["tenant_count"])
        != (e["sender_count"], e["tenant_count"])
    ):
        return _chk(
            code,
            WARN,
            "Central and Enterprise bootstrap evidence disagree.",
            central_completed=c_done,
            enterprise_completed=e_done,
            central_senders=(central_run or {}).get("sender_count"),
            enterprise_senders=(e or {}).get("sender_count"),
            central_accounts=(central_run or {}).get("tenant_count"),
            enterprise_accounts=(e or {}).get("tenant_count"),
        )
    return _chk(
        code,
        PASS,
        "Central and Enterprise bootstrap evidence agree.",
        senders=e["sender_count"],
        accounts=e["tenant_count"],
    )


def _shadow(s: dict | None) -> dict:
    if s is None:
        return _chk("shadow_evidence", UNKNOWN, "Shadow evidence was unavailable in the snapshot.")
    keys = (
        "mode",
        "comparisons_total",
        "min_samples",
        "match_rate",
        "critical_total",
        "local_allow_central_deny",
        "local_deny_central_allow",
        "central_missing",
        "policy_mismatch",
        "identity_mismatch",
        "projection_stale",
    )
    d = {k: _num(s[k]) for k in keys}
    if s["mode"] == "local" and s["comparisons_total"] == 0:
        return _chk(
            "shadow_evidence", UNKNOWN, "Shadow comparison is not active (authority is local).", **d
        )
    if s["critical_total"]:
        return _chk(
            "shadow_evidence", FAIL, f"{s['critical_total']} critical drift comparison(s).", **d
        )
    if s["comparisons_total"] < s["min_samples"]:
        return _chk(
            "shadow_evidence",
            WARN,
            f"{s['comparisons_total']} of {s['min_samples']} required samples.",
            **d,
        )
    return _chk(
        "shadow_evidence", PASS, f"{s['comparisons_total']} samples, no critical drift.", **d
    )


def _cutover(c: dict | None) -> dict:
    if c is None:
        return _chk(
            "cutover_evidence", UNKNOWN, "Cutover evidence was unavailable in the snapshot."
        )
    ev = c["latest_evidence"] or {}
    d = {
        "authority_mode": c["authority_mode"],
        "dispatch_recheck": c["dispatch_recheck"],
        "ack_configured": c["ack_configured"],
        "ack_valid": c["ack_valid"],
        "evidence_count": c["evidence_count"],
        "latest_kind": ev.get("kind"),
        "latest_hash": ev.get("hash"),
        "latest_readiness": ev.get("readiness_status"),
        "latest_created_at": ev.get("created_at"),
    }
    if c["evidence_count"] == 0:
        return _chk(
            "cutover_evidence", UNKNOWN, "No cutover evidence is recorded on Enterprise.", **d
        )
    if c["authority_mode"] == "central" and (not c["ack_configured"] or c["ack_valid"] is False):
        return _chk(
            "cutover_evidence",
            FAIL,
            "Authority is central but the cutover acknowledgement is missing or invalid.",
            **d,
        )
    if c["ack_configured"] and c["ack_valid"] is False:
        return _chk(
            "cutover_evidence", WARN, "The configured cutover acknowledgement is not valid.", **d
        )
    return _chk("cutover_evidence", PASS, "Cutover evidence is recorded.", **d)


def _alerts_summary(a: dict | None) -> dict:
    if a is None:
        return _chk("enterprise_alerts", UNKNOWN, "Alert evidence was unavailable in the snapshot.")
    n = {lv: sum(1 for i in a["items"] if i["level"] == lv) for lv in ("ok", "warning", "critical")}
    status = FAIL if n["critical"] else WARN if n["warning"] else PASS
    return _chk(
        "enterprise_alerts",
        status,
        f"{n['critical']} critical, {n['warning']} warning, {n['ok']} ok.",
        critical=n["critical"],
        warning=n["warning"],
        ok=n["ok"],
        evaluated_age_seconds=a["evaluated_age_seconds"],
    )


def build(db: Session, now: datetime, central_run: dict | None) -> dict:
    """→ {meta, metrics, checks: {section: [check]}} . Nuk ngre përjashtime për snapshot të munguar/të dëmtuar."""
    row, deployments, reporter = latest(db)
    if row is None:
        return {
            "meta": {"present": False, "deployments": 0},
            "metrics": None,
            "checks": {},
            "state": None,
        }
    meta = {
        "present": True,
        "schema_version": row.schema_version,
        "generated_at": _iso(row.generated_at),
        "received_at": _iso(row.received_at),
        "source_revision": row.source_revision,
        "deployments": deployments,
        "reporter": reporter,
        "stale_after_seconds": settings.sender_ops_stale_seconds,
    }
    try:
        if row.schema_version != v1.SCHEMA:
            raise v1.ContractError("unsupported schema version")
        data = v1.SenderOpsV1.parse(row.payload).data
    except (v1.ContractError, TypeError, KeyError):
        meta["supported"] = False
        return {"meta": meta, "metrics": None, "checks": {}, "state": "unsupported"}
    meta["supported"] = True
    state, age = freshness(row, now)
    meta.update(state=state, age_seconds=age)
    snap_status = PASS if state == "fresh" else WARN if state == "stale" else FAIL
    checks: dict[str, list[dict]] = {
        "enterprise_evidence": [
            _chk(
                "enterprise_snapshot",
                snap_status,
                {
                    "fresh": "The Enterprise snapshot is fresh.",
                    "stale": "The Enterprise snapshot is stale.",
                    "expired": "The Enterprise snapshot has expired (no recent report).",
                }[state],
                age_seconds=age,
                stale_after_seconds=settings.sender_ops_stale_seconds,
                generated_at=meta["generated_at"],
            )
        ]
    }
    derived = [
        ("sync", _sync_runtime(data["sync"])), ("requests", _request_outbox(data["requests"])), ("bootstrap", _bootstrap_categories(data["bootstrap"])),
        ("bootstrap", _agreement(data["bootstrap"], central_run)), ("enterprise_evidence", _shadow(data["shadow"])), ("enterprise_evidence", _cutover(data["cutover"])),
        ("enterprise_evidence", _alerts_summary(data["alerts"])),
        *_alert_checks(data),
    ]  # fmt: skip
    for section, c in derived:
        if state != "fresh" and c["status"] == PASS:  # s'mund të konfirmohet nga provë e vjetruar
            c = {
                **c,
                "status": UNKNOWN,
                "summary": f"Stale evidence, cannot be confirmed: {c['summary']}",
            }
        checks.setdefault(section, []).append(c)
    return {"meta": meta, "metrics": data, "checks": checks, "state": state}
