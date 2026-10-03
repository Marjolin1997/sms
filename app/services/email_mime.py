"""Ndërtimi i mesazhit RFC 5322: headers të sigurta, unsubscribe (RFC 8058), nënshkrim DKIM."""

import html as html_lib
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime, formataddr

import dkim

from app.core.config import settings
from app.core.texts import tr

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
SIGNED_HEADERS = [
    b"from", b"to", b"subject", b"date", b"message-id", b"mime-version",
    b"content-type", b"list-unsubscribe", b"list-unsubscribe-post",
]  # fmt: skip


# Pa këtë, një URL e gjatë në List-Unsubscribe kodohet si encoded-word dhe klientët e ignorojnë.
POLICY = SMTP.clone(max_line_length=998)


class UnsafeHeader(ValueError):
    pass


def clean_header(value: str, field: str) -> str:
    if _CTRL.search(value):
        raise UnsafeHeader(f"{field} contains control characters")
    return value.strip()


def unsubscribe_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/u/{token}"


def _footer_html(url: str) -> str:
    u = html_lib.escape(url, quote=True)
    style = "font-size:12px;color:#666;margin-top:24px"
    return f'<p style="{style}"><a href="{u}">{tr("Unsubscribe")}</a></p>'


def build(
    *,
    public_id: str,
    domain: str,
    from_email: str,
    from_name: str | None,
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None,
    unsubscribe_token: str | None,
    dkim_selector: str,
    dkim_private_pem: bytes,
    now: datetime | None = None,
) -> tuple[str, bytes]:
    """→ (Message-ID, bytes të nënshkruar). Ngre UnsafeHeader nëse një header ka CR/LF."""
    msg_id = f"<{public_id}@{domain}>"
    msg = EmailMessage(policy=POLICY)
    msg["From"] = (
        formataddr((clean_header(from_name, "from_name"), from_email)) if from_name else from_email
    )
    msg["To"] = clean_header(to_email, "to")
    msg["Subject"] = clean_header(subject, "subject")
    msg["Date"] = format_datetime(now or datetime.now(UTC))
    msg["Message-ID"] = msg_id
    text = text_body
    html = html_body
    if unsubscribe_token:
        url = unsubscribe_url(unsubscribe_token)
        msg["List-Unsubscribe"] = f"<{url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        text = f"{text_body}\n\n--\n{tr('Unsubscribe')}: {url}\n"
        if html:
            footer = _footer_html(url)
            idx = html.lower().rfind("</body>")
            html = html[:idx] + footer + html[idx:] if idx != -1 else html + footer
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    raw = msg.as_bytes()
    sig = dkim.sign(
        raw, dkim_selector.encode(), domain.encode(), dkim_private_pem,
        include_headers=[h for h in SIGNED_HEADERS if h in _present(raw)],
    )  # fmt: skip
    return msg_id, sig + raw


def _present(raw: bytes) -> set[bytes]:
    head = raw.split(b"\r\n\r\n", 1)[0].lower()
    return {
        line.split(b":", 1)[0]
        for line in head.split(b"\r\n")
        if b":" in line and line[:1] not in b" \t"
    }
