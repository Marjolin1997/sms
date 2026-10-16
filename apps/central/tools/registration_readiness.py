"""Readiness e regjistrimit publik për prodhim (M8-e). VETËM-LEXIM; asnjë mutacion.

    python -m apps.central.tools.registration_readiness [--strict] [--json]

Dalja: rreshta `PASS|WARN|FAIL emri: arsyeja`. Kodi: 0 nëse s'ka FAIL (me `--strict`: edhe pa WARN) ·
1 nëse ka FAIL (ose WARN me --strict) · 2 gabim i brendshëm. Kur regjistrimi publik është i fikur,
kontrollet e postës/CAPTCHA/proxy janë "n/a" (offline i qëllimshëm), por backlog-et dhe politikat
automatic kontrollohen gjithmonë. Shëndeti M7 është i dukshëm nga Central vetëm tërthorazi (aktiviteti
i fundit i konsumatorit); detajet e plota: `python -m scripts.cp_enforce_readiness` në Enterprise.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core import readiness as db_readiness
from apps.central.core import tokens
from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.product import Product, ProductStatus
from apps.central.models.registration_policy import ProductRegistrationPolicy
from apps.central.services import contact_verification, mailer, registration_ops

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _prod() -> bool:
    return settings.env == "production"


def evaluate(db: Session, *, now=None) -> list[Check]:
    out: list[Check] = []

    def add(name, level, reason):
        out.append(Check(name, level, reason))

    def strict(name, ok, reason_fail, reason_ok="ok"):  # FAIL në prodhim, WARN në dev
        add(name, PASS if ok else (FAIL if _prod() else WARN), reason_ok if ok else reason_fail)

    enabled = bool(settings.public_registration_enabled)
    add("public_registration", PASS,
        "enabled" if enabled else "disabled (offline by intent; mail/CAPTCHA/proxy checks n/a)")  # fmt: skip
    verification_ok = contact_verification.configured()
    if enabled:
        add("staff_auth", PASS if tokens.configured() else FAIL,
            "configured" if tokens.configured() else "CENTRAL_AUTH_SECRET is missing (admins cannot review)")  # fmt: skip
        if not verification_ok:
            strict("contact_verification", False,
                   "not configured (CENTRAL_REGISTRATION_VERIFY_KEY >=32 chars and CENTRAL_MAILER): "
                   "public registration would be manual-only and unverified")  # fmt: skip
        elif settings.mailer == "fake":
            strict("contact_verification", False, "CENTRAL_MAILER=fake delivers nothing")
        elif settings.mailer == "smtp" and (bad := mailer.smtp_config_problems()):
            add("contact_verification", FAIL, "; ".join(bad))
        else:
            add("contact_verification", PASS, f"configured (mailer={settings.mailer})")
        strict("proxy_limits", bool(settings.public_registration_proxy_ack),
               "CENTRAL_PUBLIC_REGISTRATION_PROXY_ACK is false: attest client_max_body_size 4k and "
               "limit_req zones for /registration* (see docs/M8_REGISTRATION.md)")  # fmt: skip
        if settings.public_registration_require_challenge and settings.bot_challenge == "disabled":
            add(
                "bot_challenge",
                FAIL,
                "challenge is required but no provider is configured (all submits denied)",
            )
        elif settings.public_registration_require_challenge:
            strict("bot_challenge", settings.bot_challenge != "fake", "fake provider in production")
        else:
            add("bot_challenge", WARN if _prod() else PASS,
                "challenge not required: abuse protection relies on edge limits + per-email quota")  # fmt: skip
        add("client_ip_trust", WARN if (_prod() and settings.trusted_proxy_hops == 0) else PASS,
            "CENTRAL_TRUSTED_PROXY_HOPS=0: client IP in logs is the proxy address"
            if settings.trusted_proxy_hops == 0 else f"{settings.trusted_proxy_hops} trusted hop(s)")  # fmt: skip
    # politikat (gjithmonë)
    rows = db.execute(
        select(ProductRegistrationPolicy, Product)
        .join(Product, Product.id == ProductRegistrationPolicy.product_id)
        .where(ProductRegistrationPolicy.self_registration_enabled.is_(True))
    ).all()
    auto = [p.code for pol, p in rows if pol.approval_mode == "automatic"]
    if auto and settings.allow_unverified_auto_registration:
        strict("automatic_policies", False,
               f"automatic policies {sorted(auto)} bypass contact verification (unverified gate is on)")  # fmt: skip
    elif auto and not verification_ok:
        add("automatic_policies", FAIL, f"automatic policies {sorted(auto)} cannot be approved safely: verification is not configured")  # fmt: skip
    else:
        add(
            "automatic_policies",
            PASS,
            f"{len(auto)} automatic policy(ies); approval only after verified contact",
        )
    retired = sorted(p.code for _, p in rows if p.status != ProductStatus.ACTIVE.value)
    add("policy_consistency", WARN if retired else PASS,
        f"self-registration enabled on retired products {retired}" if retired else "ok")  # fmt: skip
    # backlog-e
    r = registration_ops.report(db, now=now)
    pv, vf, ob, cp = r["provisioning"], r["verification"], r["email_outbox"], r["control_plane"]
    ro = registration_ops
    age = pv["failed_oldest_age_s"]
    add("failed_provisioning", PASS if not pv["failed"] else (FAIL if age and age > ro.FAILED_FAIL_S else WARN),
        f"{pv['failed']} failed (oldest {age}s): retry with reconcile_registrations --apply" if pv["failed"] else "none")  # fmt: skip
    age = pv["pending_oldest_age_s"]
    lvl = (
        PASS if not age or age <= ro.PENDING_WARN_S else (FAIL if age > ro.PENDING_FAIL_S else WARN)
    )
    add("pending_activation", lvl, f"{pv['pending']} approved awaiting provisioning (oldest {age}s)" if pv["pending"] else "none")  # fmt: skip
    age = ob["pending_oldest_age_s"]
    lvl = (
        FAIL
        if (ob["pending"] and age and age > ro.OUTBOX_FAIL_S)
        else WARN
        if (ob["failed"] or (ob["pending"] and age and age > ro.OUTBOX_WARN_S))
        else PASS
    )
    add("email_outbox", lvl, f"pending={ob['pending']} (oldest {age}s) failed={ob['failed']}")
    age = vf["unverified_oldest_age_s"]
    add("unverified_backlog", WARN if age and age > ro.UNVERIFIED_WARN_S else PASS,
        f"{vf['unverified_submitted']} unverified submitted (oldest {age}s)")  # fmt: skip
    if enabled:
        seen = cp["consumer_last_seen_age_s"]
        if cp["active_consumer_clients"] == 0:
            add(
                "control_plane",
                FAIL,
                "no active service client with an active key: Enterprise cannot receive new tenants",
            )
        elif seen is None:
            add("control_plane", WARN, "no recent consumer activity observed by Central")
        elif seen > ro.M7_STALE_FAIL_S and pv["provisioned_last_hour"]:
            add("control_plane", FAIL, f"consumer silent for {seen}s while {pv['provisioned_last_hour']} registration(s) were provisioned in the last hour")  # fmt: skip
        elif seen > ro.M7_STALE_WARN_S:
            add(
                "control_plane", WARN, f"consumer last seen {seen}s ago (SLO {ro.M7_STALE_WARN_S}s)"
            )
        else:
            add("control_plane", PASS, f"consumer active ({seen}s ago)")
    return out


def exit_code(checks: list[Check], *, strict: bool = False) -> int:
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(c.level in bad for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Registration production readiness (read-only).")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        engine = make_engine(settings.database_url)
        reason = db_readiness.check(engine)
        with Session(engine) as db:
            checks = [Check("database", FAIL if reason else PASS, reason or "schema at head")]
            if not reason:
                checks += evaluate(db)
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps([asdict(c) for c in checks]))
    else:
        for c in checks:
            print(f"{c.level} {c.name}: {c.reason}")
    return exit_code(checks, strict=a.strict)


if __name__ == "__main__":
    sys.exit(main())
