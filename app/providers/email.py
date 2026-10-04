"""Adapterë email. Provider-i merr mesazhin e ndërtuar dhe të nënshkruar (bytes RFC 5322);
kështu SMTP i çdo vendori (SES, Mailgun, SendGrid, MTA jotja) punon njësoj."""

import smtplib
import ssl
from dataclasses import dataclass
from typing import Protocol

from app.providers.base import ProviderError, SendResult


@dataclass(frozen=True)
class EmailRequest:
    reference: str  # public_id i email-it
    message_id: str  # header Message-ID (përdoret nga bounce/complaint webhooks)
    from_email: str  # envelope sender
    to_email: str
    raw: bytes


class EmailProvider(Protocol):
    name: str
    idempotent_by_reference: bool  # default False (shih `app.providers.base.is_idempotent`)

    def send(self, req: EmailRequest) -> SendResult: ...


class FakeEmailProvider:
    """Sjellja sipas pjesës lokale të adresës: temp@ → i përkohshëm, reject@ → i përhershëm."""

    name = "fake"
    idempotent_by_reference = True  # `accepted.setdefault(reference, ...)`: provuar nga kodi/testet

    def __init__(self) -> None:
        self.calls: list[EmailRequest] = []
        self.accepted: dict[str, SendResult] = {}

    def send(self, req: EmailRequest) -> SendResult:
        self.calls.append(req)
        local = req.to_email.split("@", 1)[0]
        if local == "temp":
            raise ProviderError("fake_temporary", temporary=True)
        if local == "reject":
            raise ProviderError("fake_rejected", temporary=False)
        return self.accepted.setdefault(req.reference, SendResult(req.message_id))


class SmtpEmailProvider:
    """SMTP me STARTTLS (ose SMTP_SSL në 465). 4xx → retry; 5xx → i përhershëm.

    `idempotent_by_reference = False`: SMTP s'ka idempotencë (Message-ID-ja deterministike
    s'garanton dedup te marrësi). Gabim pas lidhjes (disconnect/timeout gjatë DATA) ⇒
    `ambiguous=True` ⇒ UNKNOWN."""

    idempotent_by_reference = False

    def __init__(
        self,
        name: str,
        host: str,
        port: int = 587,
        user: str = "",
        password: str = "",
        starttls: bool = True,
        timeout: float = 15.0,
    ) -> None:
        self.name, self._host, self._port = name, host, port
        self._user, self._password, self._starttls, self._timeout = (
            user,
            password,
            starttls,
            timeout,
        )

    def send(self, req: EmailRequest) -> SendResult:
        ctx = ssl.create_default_context()
        try:
            try:
                if self._port == 465:
                    smtp = smtplib.SMTP_SSL(
                        self._host, self._port, timeout=self._timeout, context=ctx
                    )
                else:
                    smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            except OSError as e:  # lidhja s'u vendos: s'u dërgua asgjë
                raise ProviderError(f"smtp_connect:{type(e).__name__}", temporary=True) from e
            with smtp:
                if self._port != 465 and self._starttls:
                    smtp.starttls(context=ctx)
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.sendmail(req.from_email, [req.to_email], req.raw)
        except smtplib.SMTPRecipientsRefused as e:
            raise ProviderError("smtp_recipient_refused", temporary=_all_4xx(e.recipients)) from e
        except smtplib.SMTPResponseException as e:
            raise ProviderError(f"smtp_{e.smtp_code}", temporary=400 <= e.smtp_code < 500) from e
        except ProviderError:
            raise
        except (smtplib.SMTPException, OSError) as e:  # pas lidhjes: mund të jetë pranuar
            raise ProviderError(
                f"smtp_error:{type(e).__name__}", temporary=True, ambiguous=True
            ) from e
        return SendResult(req.message_id)


def _all_4xx(recipients: dict) -> bool:
    return bool(recipients) and all(400 <= code < 500 for code, _ in recipients.values())
