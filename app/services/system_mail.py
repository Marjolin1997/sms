"""Email të sistemit (jo të klientëve): rivendosje fjalëkalimi dhe njoftim ndryshimi.

Dërgohen pas përgjigjes (BackgroundTasks), që koha e përgjigjes të mos zbulojë nëse një email
ekziston dhe një SMTP i rënë të mos e prishë kërkesën. Dështimi regjistrohet, s'ngrihet."""

import logging
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.core.config import settings
from app.providers import ProviderError, get_email_provider
from app.providers.email import EmailRequest

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.system_from_email)


def _send(to: str, subject: str, text: str, html: str) -> None:
    if not enabled():
        return
    domain = settings.system_from_email.split("@", 1)[-1]
    msg = EmailMessage()
    msg["From"] = formataddr((settings.system_from_name, settings.system_from_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = mid = make_msgid(domain=domain)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    try:
        get_email_provider(settings.email_provider).send(
            EmailRequest(
                reference=str(uuid.uuid4()),
                message_id=mid,
                from_email=settings.system_from_email,
                to_email=to,
                raw=msg.as_bytes(),
            )  # fmt: skip
        )
    except ProviderError as e:
        log.warning("system email to user failed: %s", e)


def _html(body: str) -> str:
    return f"<div style='font-family:sans-serif;max-width:480px;line-height:1.5'>{body}</div>"


def reset_link(token: str) -> str:
    return f"{settings.panel_url.rstrip('/')}/#accept/{token}"


def send_reset(to: str, token: str, minutes: int) -> None:
    url = reset_link(token)
    what = "reset your password"
    text = (
        f"Someone asked to {what} on {settings.system_from_name}.\n\n"
        f"Open this link (valid for {minutes} minutes, works once):\n{url}\n\n"
        "If this wasn't you, ignore this email. Your password stays the same."
    )
    html = _html(
        f"<p>Someone asked to {what} on {settings.system_from_name}.</p>"
        f"<p><a href='{url}' style='background:#4f46e5;color:#fff;padding:10px 16px;"
        f"border-radius:8px;text-decoration:none'>Choose a new password</a></p>"
        f"<p>The link is valid for {minutes} minutes and works once.</p>"
        "<p style='color:#6b7280'>If this wasn't you, ignore this email. "
        "Your password stays the same.</p>"
    )
    _send(to, f"{settings.system_from_name}: {what}", text, html)


def send_password_changed(to: str) -> None:
    text = (
        f"The password for your {settings.system_from_name} account was just changed.\n\n"
        "If this was you, nothing else to do. If not, ask your administrator to reset it "
        "and disable the account right away."
    )
    html = _html(
        f"<p>The password for your {settings.system_from_name} account was just changed.</p>"
        "<p>If this was you, nothing else to do. If not, contact your administrator "
        "immediately.</p>"
    )
    _send(to, f"{settings.system_from_name}: your password was changed", text, html)
