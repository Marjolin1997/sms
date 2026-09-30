"""Pagesa online. Shuma dhe monedha vendosen nga serveri; webhook-u verifikohet kundrejt tyre.
Para nuk humbet kurrë: një pagesë që s'mund t'i aplikohet një fature bëhet kredit në wallet."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
)
from app.models.wallet import TopupMethod, Wallet
from app.providers import ProviderError
from app.providers.payments import get_gateway
from app.services import billing, events
from app.services import wallet as wallets
from app.services.wallet import Conflict, InvalidAmount, NotFound, WalletError


class GatewayError(WalletError):
    code = "gateway_error"


class PaymentsDisabled(WalletError):
    code = "payments_disabled"


def start_payment(
    db: Session, owner_ref: str, purpose: str, amount=None, invoice_id: int | None = None,
    wallet_id: int | None = None,
) -> Payment:  # fmt: skip
    purpose = PaymentPurpose(purpose)
    if purpose == PaymentPurpose.INVOICE:
        if invoice_id is None:
            raise InvalidAmount("invoice_id is required")
        inv = billing._get_invoice(db, owner_ref, invoice_id, lock=True)
        if inv.status != InvoiceStatus.OPEN:
            raise Conflict(f"invoice is {inv.status.value}")
        existing = db.scalar(
            select(Payment).where(
                Payment.invoice_id == inv.id, Payment.status == PaymentStatus.PENDING
            )
        )
        if existing:
            return existing  # e njëjta faturë s'ka dy seanca të hapura
        amt, currency, w_id = inv.total, inv.currency, None
        desc = f"Invoice {inv.number}"
    else:
        w = db.scalar(select(Wallet).where(Wallet.id == wallet_id, Wallet.owner_ref == owner_ref))
        if w is None:
            raise NotFound("wallet not found")
        amt = wallets.positive(amount if amount is not None else 0)
        if not Decimal(settings.payment_min) <= amt <= Decimal(settings.payment_max):
            raise InvalidAmount(
                f"amount must be between {settings.payment_min} and {settings.payment_max}"
            )
        currency, w_id, inv = w.currency, w.id, None
        desc = f"Wallet top-up {currency}"
    if settings.payment_provider == "disabled":
        raise PaymentsDisabled("online payments are not enabled; contact us to top up")
    try:
        gw = get_gateway(settings.payment_provider)
        session = gw.create_checkout(f"{owner_ref}:{purpose.value}", amt, currency, desc)
    except ProviderError as e:
        raise GatewayError(f"payment gateway unavailable: {e.code}") from e
    p = Payment(
        owner_ref=owner_ref, purpose=purpose, invoice_id=inv.id if inv else None, wallet_id=w_id,
        amount=amt, currency=currency, provider=gw.name, external_id=session.external_id,
        checkout_url=session.url,
    )  # fmt: skip
    db.add(p)
    db.flush()
    return p


def complete(
    db: Session, provider: str, external_id: str, status: str, amount: str | None,
    currency: str | None, now: datetime | None = None,
) -> Payment:  # fmt: skip
    """Webhook i gateway-t. Idempotent; verifikon shumën dhe monedhën kundrejt serverit."""
    now = as_utc(now or datetime.now(UTC))
    p = db.scalar(
        select(Payment)
        .where(Payment.provider == provider, Payment.external_id == external_id)
        .with_for_update()
    )
    if p is None:
        raise NotFound("payment not found")
    if status == "failed":
        if p.status == PaymentStatus.SUCCEEDED:
            raise Conflict("payment already succeeded")
        if p.status != PaymentStatus.FAILED:
            p.status, p.failure_reason, p.completed_at = PaymentStatus.FAILED, "gateway_failed", now
            events.emit(db, p.owner_ref, "payment.failed", "payment", p.id,
                        {"payment_id": p.id, "purpose": p.purpose.value})  # fmt: skip
        return p
    if p.status == PaymentStatus.SUCCEEDED:
        return p
    if p.status == PaymentStatus.FAILED:
        raise Conflict("payment already marked failed")
    try:
        paid = Decimal(str(amount))
    except (InvalidOperation, TypeError):
        paid = None
    if paid != p.amount or (currency or "").upper() != p.currency:
        # Para mund të kenë ardhur me shumë tjetër: s'kreditojmë automatikisht, kërkon rakordim.
        p.status, p.failure_reason, p.completed_at = PaymentStatus.FAILED, "amount_mismatch", now
        raise Conflict("amount or currency does not match the payment; needs manual reconciliation")
    _apply(db, p, now)
    p.status, p.completed_at = PaymentStatus.SUCCEEDED, now
    events.emit(db, p.owner_ref, "payment.succeeded", "payment", p.id,
                {"payment_id": p.id, "purpose": p.purpose.value, "amount": str(p.amount),
                 "currency": p.currency})  # fmt: skip
    return p


def _apply(db: Session, p: Payment, now: datetime) -> None:
    if p.purpose == PaymentPurpose.INVOICE:
        inv = db.scalar(select(Invoice).where(Invoice.id == p.invoice_id).with_for_update())
        if inv is not None and inv.status == InvoiceStatus.OPEN:
            billing.mark_paid_online(db, inv, now)
            return
        # fatura u pagua ose u anulua ndërkohë: paraja kalon në wallet, s'humbet
        wallet = db.scalar(
            select(Wallet).where(Wallet.owner_ref == p.owner_ref, Wallet.currency == p.currency)
        )
        if wallet is None:
            wallet = wallets.create_wallet(db, p.owner_ref, p.currency)
        wallet_id = wallet.id
        p.failure_reason = "invoice_already_settled"
    else:
        wallet_id = p.wallet_id
    t = wallets.create_topup(
        db, wallet_id, p.amount, TopupMethod.ELECTRONIC,
        external_ref=f"{p.provider}:{p.external_id}",
        created_by=f"payment:{p.id}",
    )  # fmt: skip
    wallets.confirm_topup(db, t.id)


def expire_pending(db: Session, older_than: timedelta = timedelta(hours=24), now=None) -> int:
    now = as_utc(now or datetime.now(UTC))
    rows = db.scalars(
        select(Payment)
        .where(Payment.status == PaymentStatus.PENDING, Payment.created_at < now - older_than)
        .with_for_update(skip_locked=True)
    ).all()
    for p in rows:
        p.status, p.failure_reason = PaymentStatus.EXPIRED, "timeout"
    return len(rows)
