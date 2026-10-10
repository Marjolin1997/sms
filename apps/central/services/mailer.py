"""Mailer i Central (M8-e) — kufi i ngushtë, i PAVARUR nga Enterprise (pa import `app.*`).

`RegistrationMailer.send_verification(...)` dërgon VETËM email-in e verifikimit të kontaktit. Gabimet
kthehen si `MailerError(code, temporary)` me kod të qëndrueshëm (kurrë tekst SMTP/kredenciale).
Implementime: `FakeMailer` (test/dev; refuzohet në prodhim), `SmtpMailer` (stdlib `smtplib`).
`mailer=disabled` (default) ⇒ `get_mailer()` kthen None: verifikimi i padisponueshëm.
Dërgimi bëhet gjithmonë JASHTË transaksionit të DB (shih `notifications.dispatch_due`).
"""

import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import quote

from apps.central.core.config import settings


class MailerError(Exception):
    def __init__(self, code: str, *, temporary: bool):
        super().__init__(code)
        self.code, self.temporary = code, temporary


class RegistrationMailer(Protocol):
    name: str

    def send_verification(
        self, *, to: str, registration_id: str, token: str, expires_at: datetime
    ) -> None: ...


@dataclass
class FakeMailer:
    """Regjistron mesazhet; sjellje e kontrollueshme: `fail_with` (MailerError) ose `fail_next`."""

    name: str = "fake"
    sent: list = field(default_factory=list)
    fail_with: MailerError | None = None

    def send_verification(self, *, to, registration_id, token, expires_at) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(
            {"to": to, "registration_id": registration_id, "token": token, "expires_at": expires_at}
        )


def verification_link(registration_id: str, token: str) -> str:
    base = settings.registration_verify_url_base.rstrip("?&")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}id={quote(registration_id)}&token={quote(token)}"


class SmtpMailer:
    name = "smtp"

    def send_verification(self, *, to, registration_id, token, expires_at) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"] = settings.smtp_from, to
        msg["Subject"] = "Confirm your email address"
        link = verification_link(registration_id, token)
        msg.set_content(
            "Please confirm your email address to continue your registration:\n\n"
            f"{link}\n\nThis link expires at {expires_at:%Y-%m-%d %H:%M} UTC. "
            "If you did not request this, ignore this message."
        )
        try:
            if settings.smtp_starttls:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
                    s.starttls(context=ssl.create_default_context())
                    self._deliver(s, msg)
            else:
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
                    self._deliver(s, msg)
        except smtplib.SMTPRecipientsRefused:
            raise MailerError("recipient_refused", temporary=False) from None
        except smtplib.SMTPResponseException as e:
            raise MailerError("smtp_rejected", temporary=400 <= e.smtp_code < 500) from None
        except (OSError, smtplib.SMTPException):
            raise MailerError("smtp_unavailable", temporary=True) from None

    @staticmethod
    def _deliver(smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


_FAKE = FakeMailer()


def get_mailer() -> RegistrationMailer | None:
    if settings.mailer == "fake":
        return _FAKE
    if settings.mailer == "smtp":
        return SmtpMailer()
    return None


def smtp_config_problems() -> list[str]:
    """Probleme të konfigurimit SMTP (për readiness/startim): [] kur është i plotë."""
    bad = []
    if not settings.smtp_host:
        bad.append("CENTRAL_SMTP_HOST is empty")
    if not settings.smtp_from or "@" not in settings.smtp_from:
        bad.append("CENTRAL_SMTP_FROM must be a sender address")
    if not settings.registration_verify_url_base.startswith("https://"):
        bad.append("CENTRAL_REGISTRATION_VERIFY_URL_BASE must be an https:// URL")
    return bad
