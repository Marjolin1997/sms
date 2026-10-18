"""Roja "pa ofrues të vërtetë" e M11-H: dështon shpejt nëse mjedisi/konfigurimi/rrugët lejojnë një dërgim të jashtëm (SMS, email, pagesë).

Përdoret nga çdo skript pilot (importohet nga `lib`) dhe testohet te `tests/test_m11h_pilot_guard.py`.
"""

import os
import re
from collections.abc import Mapping

REAL_SETTINGS = ("twilio_account_sid", "twilio_auth_token", "http_provider_url", "smtp_host")
FAKE_ONLY = {"email_provider": {"fake"}, "payment_provider": {"fake", "disabled"}}
ENV_PATTERNS = re.compile(
    r"^(SMS_)?(TWILIO_|HTTP_PROVIDER_URL|SMTP_HOST|SMTP_USER|SMTP_PASSWORD|VONAGE|INFOBIP|SMPP|STRIPE|PAYPAL)",
    re.I,
)


class RealProviderConfigured(SystemExit):
    pass


def violations(env: Mapping[str, str], settings=None, route_providers=None) -> list[str]:
    out = [
        f"env {k} is set"
        for k, v in env.items()
        if ENV_PATTERNS.match(k) and v and not k.endswith("_PROVIDER")
    ]
    if settings is not None:
        out += [f"setting {n} is configured" for n in REAL_SETTINGS if getattr(settings, n, "")]
        out += [
            f"setting {n}={getattr(settings, n, None)!r} is not a fake"
            for n, ok in FAKE_ONLY.items()
            if getattr(settings, n, None) not in ok
        ]
    if route_providers is not None and set(route_providers) - {"fake"}:
        out.append(f"routes use non-fake providers: {sorted(set(route_providers) - {'fake'})}")
    return out


def assert_fake_only(env=None) -> None:
    env = os.environ if env is None else env
    problems = violations(env)
    try:
        from app.core.config import settings

        problems += violations({}, settings)
    except Exception:  # pragma: no cover - harness without app import
        pass
    url = env.get("SMS_DATABASE_URL", "")
    if url.startswith("postgresql"):
        import psycopg

        with psycopg.connect(url.replace("+psycopg", "")) as c:
            provs = [r[0] for r in c.execute("select distinct provider from sms_routes")]
        problems += violations({}, None, provs)
    if problems:
        raise RealProviderConfigured(
            "REFUSING TO RUN: real provider configuration detected: " + "; ".join(problems)
        )
