"""M11-I/F-07A: gate GO/NO-GO i provider-it/rrugës dhe plani i rikthimit. VETËM-LEXIM (asnjë commit, asnjë thirrje provider-i).

    python -m scripts.provider_readiness check --provider NAME [--phase pre|post] [--hours 24]
        [--min-sample 20] [--min-delivery-rate 0.9] [--max-unknown 0] [--max-sent-age-seconds 3600]
        [--max-queued-age-seconds 900] [--strict] [--json]
    python -m scripts.provider_readiness rollback-plan --provider NAME [--json]

`check --phase pre` (para canary-t): provider i regjistruar (konfiguruar), sekret DLR/token i fortë, URL publike https, rrugë
e aktivizuar që FITON për prefiksin (prioritet), deklarimi i idempotencës, kill switch-et, UNKNOWN/SENDING i ngecur.
`check --phase post` (gjatë/pas canary-t): shton matjet e dritares (`--hours`): shkalla e dorëzimit (me kampion minimal),
UNKNOWN, SENT pa DLR, backlog në radhë, ID provider-i të dyfishta, probleme DLR (conflict/unknown_message), presion riprovash.
Kodi: 0 GO (pa FAIL; me `--strict` edhe pa WARN) · 1 NO-GO · 2 gabim. NUK provon arritshmërinë publike të callback-ut, DNS/TLS real,
kredencialet reale apo dorëzimin real: ato janë F-07B (PRODUCTION-ONLY). Dalja nuk përmban sekrete, numra apo tekst mesazhesh."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.core.config import _weak_secret, settings
from app.core.db import SessionLocal
from app.models.sending import DlrReceipt, Message, MessageStatus, Route
from app.providers import _registry, register_configured
from app.providers.base import is_idempotent
from app.services import switches
from app.services.messages import DUPLICATE_PROVIDER_ID

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
AUTH_CODES = ("twilio_auth", "twilio_forbidden", "http_401", "http_403")
IN_FLIGHT = (MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT, MessageStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _age(now: datetime, ts) -> int | None:
    if ts is None:
        return None
    ts = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return max(0, int((now - ts).total_seconds()))


def _routes(db, provider: str) -> list[Route]:
    return list(
        db.scalars(
            select(Route)
            .where(Route.provider == provider, Route.enabled.is_(True))
            .order_by(Route.prefix)
        )
    )


def config_checks(db, provider: str, *, phase: str, registry=None) -> list[Check]:
    registry = _registry if registry is None else registry
    out: list[Check] = []
    prod = settings.env == "production"
    impl = registry.get(provider)
    out.append(
        Check(
            "provider_registered",
            PASS if impl else FAIL,
            "registered"
            if impl
            else "not registered (configure its SMS_* variables and restart the worker)",
        )
    )
    if provider == "fake":
        out.append(
            Check(
                "provider_not_fake",
                FAIL if prod else WARN,
                "the fake provider must never carry production traffic",
            )
        )
    if impl is not None:
        idem = is_idempotent(impl)
        out.append(
            Check(
                "idempotency_declared",
                PASS,
                f"idempotent_by_reference={idem}: "
                + (
                    "ambiguous outcomes are retried with the same reference"
                    if idem
                    else "ambiguous outcomes become UNKNOWN and are never auto-resent"
                ),
            )
        )
    if provider == "twilio":
        ok = bool(settings.twilio_account_sid and settings.twilio_auth_token)
        out.append(
            Check(
                "callback_auth",
                PASS if ok else FAIL,
                "SMS_TWILIO_ACCOUNT_SID/AUTH_TOKEN set"
                if ok
                else "SMS_TWILIO_ACCOUNT_SID and SMS_TWILIO_AUTH_TOKEN must both be set",
            )
        )
    else:
        weak = _weak_secret(settings.dlr_secrets.get(provider))
        out.append(
            Check(
                "callback_auth",
                FAIL if weak else PASS,
                f"SMS_DLR_SECRETS['{provider}'] "
                + (
                    "is missing or weak (24+ random chars required)"
                    if weak
                    else "is set and strong"
                ),
            )
        )
    https = settings.public_base_url.startswith("https://")
    out.append(
        Check(
            "callback_url_scheme",
            PASS if https else (FAIL if prod else WARN),
            "SMS_PUBLIC_BASE_URL is https (reachability from the provider is NOT verified here: F-07B)"
            if https
            else "SMS_PUBLIC_BASE_URL is not https",
        )
    )
    routes = _routes(db, provider)
    out.append(
        Check(
            "route_enabled",
            PASS if routes else FAIL,
            f"{len(routes)} enabled route(s): "
            + ", ".join(f"{r.prefix}@{r.priority}" for r in routes[:10])
            if routes
            else "no enabled route for this provider",
        )
    )
    loses = []
    for r in routes:
        rivals = db.scalars(
            select(Route).where(
                Route.prefix == r.prefix,
                Route.enabled.is_(True),
                Route.provider != provider,
                Route.priority >= r.priority,
            )
        ).all()
        if rivals:
            loses.append(r.prefix)
    if routes:
        out.append(
            Check(
                "route_wins_precedence",
                WARN if loses else PASS,
                (
                    "another enabled route has equal/higher priority for prefix(es): "
                    + ", ".join(loses[:10])
                )
                if loses
                else "no competing route with equal/higher priority",
            )
        )
    rate, conc = (
        settings.provider_rate_per_sec.get(provider),
        settings.provider_max_concurrency.get(provider),
    )
    out.append(
        Check(
            "throttle_config", PASS,
            f"rate_per_sec={rate or 'unlimited'} (per worker process), max_concurrency={conc or 'unlimited'} (DB-wide, best-effort)",
        )
    )  # fmt: skip
    for name in sorted(switches.NAMES):
        on = switches.is_enabled(db, name)
        out.append(
            Check(
                f"switch_{name}",
                PASS if on else (FAIL if phase == "pre" else WARN),
                "enabled" if on else "PAUSED",
            )
        )
    return out


def metric_checks(
    db,
    provider: str,
    *,
    hours: int,
    min_sample: int,
    min_rate: float,
    max_unknown: int,
    max_sent_age: int,
    max_queued_age: int,
    now: datetime,
) -> list[Check]:
    since = now - timedelta(hours=hours)
    base = (Message.provider == provider, Message.created_at > since)
    counts = {
        st: n
        for st, n in db.execute(
            select(Message.status, func.count()).where(*base).group_by(Message.status)
        )
    }
    total = sum(counts.values())
    delivered, failed = counts.get(MessageStatus.DELIVERED, 0), counts.get(MessageStatus.FAILED, 0)
    out = [
        Check(
            "window_sample",
            PASS if total else WARN,
            f"{total} message(s) in {hours}h: "
            + ", ".join(
                f"{k.value}={v}" for k, v in sorted(counts.items(), key=lambda kv: kv[0].value)
            )
            if total
            else f"no messages in the last {hours}h (nothing to judge yet)",
        )
    ]
    terminal = delivered + failed
    if terminal < min_sample:
        out.append(
            Check(
                "delivery_rate",
                WARN,
                f"only {terminal} terminal message(s) (< {min_sample}): sample too small for a rate; require every canary message to be delivered",
            )
        )
    else:
        rate = delivered / terminal
        out.append(
            Check(
                "delivery_rate",
                PASS if rate >= min_rate else FAIL,
                f"{rate:.1%} delivered of {terminal} terminal (min {min_rate:.0%})",
            )
        )
    unknown = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.provider == provider, Message.status == MessageStatus.UNKNOWN)
    )
    out.append(
        Check(
            "unknown_outcomes",
            PASS if unknown <= max_unknown else FAIL,
            f"{unknown} UNKNOWN (max {max_unknown}); UNKNOWN is resolved by an authoritative DLR or staff, never auto-resent",
        )
    )
    stale_sent = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.provider == provider,
            Message.status == MessageStatus.SENT,
            Message.updated_at <= now - timedelta(seconds=max_sent_age),
        )
    )
    out.append(
        Check(
            "sent_without_dlr",
            PASS if not stale_sent else WARN,
            f"{stale_sent} SENT message(s) older than {max_sent_age}s without a DLR",
        )
    )
    oldest_q = db.scalar(
        select(func.min(Message.next_attempt_at)).where(
            Message.provider == provider, Message.status == MessageStatus.QUEUED
        )
    )
    age = _age(now, oldest_q)
    out.append(
        Check(
            "queue_backlog",
            WARN if age is not None and age > max_queued_age else PASS,
            "empty"
            if age is None
            else f"oldest queued message is due since {age}s (max {max_queued_age}s)",
        )
    )
    dup = db.scalar(
        select(func.count()).select_from(
            select(Message.provider_message_id)
            .where(Message.provider == provider, Message.provider_message_id.is_not(None))
            .group_by(Message.provider_message_id)
            .having(func.count() > 1)
            .subquery()
        )
    )
    out.append(
        Check(
            "duplicate_provider_ids",
            PASS if not dup else FAIL,
            f"{dup} provider message id(s) shared by several messages" if dup else "none",
        )
    )
    dupc = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.provider == provider,
            Message.error_code == DUPLICATE_PROVIDER_ID,
            Message.status == MessageStatus.UNKNOWN,
        )
    )
    out.append(
        Check(
            "duplicate_provider_id_conflicts",
            PASS if not dupc else WARN,
            f"{dupc} message(s) UNKNOWN: the provider returned an id owned by another message; reconcile with its panel (never resend)"
            if dupc
            else "none",
        )
    )
    auth = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            *base,
            Message.status == MessageStatus.FAILED,
            or_(Message.error_code.in_(AUTH_CODES), Message.error_code.like("twilio_200%")),
        )
    )
    out.append(
        Check(
            "provider_auth_failures",
            PASS if not auth else FAIL,
            f"{auth} message(s) failed with an authentication/permission error ({', '.join(AUTH_CODES)}): fix credentials, pause dispatch"
            if auth
            else "none",
        )
    )
    recent = or_(DlrReceipt.received_at > since, DlrReceipt.last_seen_at > since)
    probs = {
        o: int(n)
        for o, n in db.execute(
            select(DlrReceipt.outcome, func.coalesce(func.sum(DlrReceipt.seen_count), 0))
            .where(DlrReceipt.provider == provider, DlrReceipt.outcome != "applied", recent)
            .group_by(DlrReceipt.outcome)
        )
    }
    replays = db.scalar(
        select(func.coalesce(func.sum(DlrReceipt.seen_count - 1), 0)).where(
            DlrReceipt.provider == provider, recent
        )
    )
    out.append(
        Check(
            "dlr_replays",
            PASS,
            f"{int(replays or 0)} deduplicated replayed/retried callback(s) (idempotent, no side effect)",
        )
    )
    out.append(
        Check(
            "dlr_problems",
            PASS if not probs else WARN,
            ", ".join(f"{k}={v}" for k, v in sorted(probs.items())) if probs else "none",
        )
    )
    retried = db.scalar(
        select(func.count()).select_from(Message).where(*base, Message.attempts > 1)
    )
    out.append(
        Check(
            "retry_pressure",
            PASS if not total or retried <= max(1, total // 10) else WARN,
            f"{retried} of {total} message(s) needed more than one attempt",
        )
    )
    return out


def evaluate(
    db,
    provider: str,
    *,
    phase: str = "pre",
    hours: int = 24,
    min_sample: int = 20,
    min_rate: float = 0.9,
    max_unknown: int = 0,
    max_sent_age: int = 3600,
    max_queued_age: int = 900,
    now: datetime | None = None,
    registry=None,
) -> list[Check]:
    from scripts import queue_readiness

    now = now or datetime.now(UTC)
    out = config_checks(db, provider, phase=phase, registry=registry)
    out += [
        Check(c.name, c.level, c.reason)
        for c in queue_readiness.evaluate(db, now=now)
        if c.name.startswith(("stuck_sending", "unknown_"))
    ]
    if phase == "post":
        out += metric_checks(
            db,
            provider,
            hours=hours,
            min_sample=min_sample,
            min_rate=min_rate,
            max_unknown=max_unknown,
            max_sent_age=max_sent_age,
            max_queued_age=max_queued_age,
            now=now,
        )
    return out


def verdict(checks, *, strict: bool = False) -> str:
    bad = {FAIL, WARN} if strict else {FAIL}
    return "NO-GO" if any(c.level in bad for c in checks) else "GO"


def duplicate_groups(db, limit: int = 200) -> list[dict]:
    """Vetëm lexim: grupe (provider, provider_message_id) të ndara nga ≥ 2 mesazhe (histori para M11-J). Pa tekst/numra."""
    grp = (
        select(Message.provider, Message.provider_message_id, func.count().label("n"))
        .where(Message.provider_message_id.is_not(None))
        .group_by(Message.provider, Message.provider_message_id)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(limit)
        .subquery()
    )
    out = []
    for prov, pid, n in db.execute(select(grp.c.provider, grp.c.provider_message_id, grp.c.n)):
        ids = db.scalars(
            select(Message.public_id)
            .where(Message.provider == prov, Message.provider_message_id == pid)
            .order_by(Message.id)
        ).all()
        out.append(
            {
                "provider": prov,
                "provider_message_id": pid,
                "messages": int(n),
                "public_ids": list(ids),
            }
        )
    return out


ROLLBACK_STEPS = (
    "1. Stop new canary traffic: disable the provider's routes (PUT /v1/admin/routes enabled=false) and, if needed, pause `dispatch` (kill switch).",
    "2. Restore the previous route/provider (re-enable the previous route with a higher priority) and re-run `provider_readiness check` for it.",
    "3. Leave in-flight messages alone: QUEUED drain via the restored route or are cancelled by staff; SENT wait for their DLR (or dlr_timeout).",
    "4. NEVER resend UNKNOWN messages: reconcile each against the provider's panel, then resolve (billable_delivered | non_billable_failed) or attach the provider id.",
    "5. Verify reconciliation: queue_readiness PASS, wallet ledgers verify (scripts.verify_ledger), UNKNOWN list reviewed, DLR problems explained.",
    "6. Preserve evidence (readiness JSON before/after, UNKNOWN resolutions, DlrReceipt summary) in the incident record.",
)  # fmt: skip


def rollback_plan(db, provider: str, *, now: datetime | None = None) -> dict:
    """Vetëm lexim: çfarë është në ajër për këtë provider dhe hapat e rikthimit. Pa numra/tekst; asnjë mutacion."""
    now = now or datetime.now(UTC)
    by = {st.value: 0 for st in IN_FLIGHT}
    for st, n in db.execute(
        select(Message.status, func.count())
        .where(Message.provider == provider, Message.status.in_(IN_FLIGHT))
        .group_by(Message.status)
    ):
        by[st.value] = n
    oldest_unknown = db.scalar(
        select(func.min(Message.updated_at)).where(
            Message.provider == provider, Message.status == MessageStatus.UNKNOWN
        )
    )
    return {
        "provider": provider,
        "enabled_routes": [
            {"prefix": r.prefix, "priority": r.priority} for r in _routes(db, provider)
        ],
        "switches": {n: switches.is_enabled(db, n) for n in sorted(switches.NAMES)},
        "in_flight": by,
        "unknown_oldest_age_seconds": _age(now, oldest_unknown),
        "must_not_resend": by["unknown"],
        "steps": list(ROLLBACK_STEPS),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Provider/route GO-NO-GO gate and rollback plan (read-only)."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--provider", required=True)
    c.add_argument("--phase", choices=("pre", "post"), default="pre")
    c.add_argument("--hours", type=int, default=24)
    c.add_argument("--min-sample", type=int, default=20)
    c.add_argument("--min-delivery-rate", type=float, default=0.9)
    c.add_argument("--max-unknown", type=int, default=0)
    c.add_argument("--max-sent-age-seconds", type=int, default=3600)
    c.add_argument("--max-queued-age-seconds", type=int, default=900)
    c.add_argument("--strict", action="store_true")
    d = sub.add_parser(
        "duplicate-ids", help="list historical duplicate provider message ids (read-only)"
    )
    d.add_argument("--json", action="store_true")
    r = sub.add_parser("rollback-plan")
    r.add_argument("--provider", required=True)
    for p in (c, r):
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        register_configured()  # vetëm regjistron adapterët e konfiguruar; asnjë kërkesë rrjeti
        with SessionLocal() as db:
            try:
                if a.cmd == "duplicate-ids":
                    groups = duplicate_groups(db)
                    if a.json:
                        print(json.dumps(groups, indent=2))
                    else:
                        for g in groups:
                            print(
                                f"{g['provider']} {g['provider_message_id']}: {g['messages']} messages {g['public_ids']}"
                            )
                        print(f"{len(groups)} duplicate group(s)")
                    return 1 if groups else 0
                if a.cmd == "rollback-plan":
                    plan = rollback_plan(db, a.provider)
                    if a.json:
                        print(json.dumps(plan, indent=2))
                    else:
                        print(
                            f"provider={plan['provider']} routes={plan['enabled_routes']} switches={plan['switches']}"
                        )
                        print(
                            f"in_flight={plan['in_flight']} must_not_resend={plan['must_not_resend']}"
                        )
                        print("\n".join(plan["steps"]))
                    return 0
                checks = evaluate(
                    db,
                    a.provider,
                    phase=a.phase,
                    hours=a.hours,
                    min_sample=a.min_sample,
                    min_rate=a.min_delivery_rate,
                    max_unknown=a.max_unknown,
                    max_sent_age=a.max_sent_age_seconds,
                    max_queued_age=a.max_queued_age_seconds,
                )
            finally:
                db.rollback()  # asnjë shkrim kurrë
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}", file=sys.stderr)
        return 2
    v = verdict(checks, strict=a.strict)
    if a.json:
        print(json.dumps({"verdict": v, "checks": [asdict(x) for x in checks]}, indent=2))
    else:
        for x in checks:
            print(f"{x.level} {x.name}: {x.reason}")
        print(f"verdict: {v}")
    return 0 if v == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
