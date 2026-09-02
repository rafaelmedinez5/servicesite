from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from enum import StrEnum
from typing import Callable, Protocol

from app.catalog import PurchasableService
from app.checkout_details import CheckoutDetails
from app.orders import MAX_CART_SERVICES, OrderLine
from app.payments.xmr_wallet_rpc import (
    ATOMIC_UNITS_PER_XMR,
    MAX_ATOMIC_UNITS,
    XmrSubaddress,
)


DEFAULT_INVOICE_TTL = timedelta(hours=2)
_INVOICE_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class InvoiceError(RuntimeError):
    """Base class for invoice-domain failures."""


class InvoiceValidationError(InvoiceError):
    """Invoice input or persisted data violates a domain invariant."""


class ServiceUnavailableError(InvoiceError):
    """The selected catalog service is not currently purchasable."""


class IllegalInvoiceTransition(InvoiceError):
    """The requested payment-state transition is not permitted."""


class PaymentStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    PAID_PENDING_CONFIRMATIONS = "paid_pending_confirmations"
    PAID_PENDING_SWEEP = "paid_pending_sweep"
    SWEEPING_TO_COLD = "sweeping_to_cold"
    SETTLED = "settled"
    EXPIRED = "expired"


STATUS_NOTES = {
    PaymentStatus.AWAITING_PAYMENT: "Waiting for payment.",
    PaymentStatus.PAID_PENDING_CONFIRMATIONS: "Payment detected. Waiting for confirmations.",
    PaymentStatus.PAID_PENDING_SWEEP: "Payment confirmed. Finalizing payment.",
    PaymentStatus.SWEEPING_TO_COLD: "Payment confirmed. Finalizing payment.",
    PaymentStatus.SETTLED: "Payment confirmed.",
    PaymentStatus.EXPIRED: "Payment window expired.",
}


@dataclass(frozen=True)
class XmrQuote:
    usd_per_xmr: Decimal
    source: str
    quoted_at: datetime

    @classmethod
    def from_string(cls, usd_per_xmr: str, *, source: str, quoted_at: datetime) -> "XmrQuote":
        if not isinstance(usd_per_xmr, str) or not usd_per_xmr.strip():
            raise InvoiceValidationError("XMR/USD rate must be a decimal string")
        try:
            rate = Decimal(usd_per_xmr.strip())
        except InvalidOperation as exc:
            raise InvoiceValidationError("XMR/USD rate is invalid") from exc
        return cls(usd_per_xmr=rate, source=source, quoted_at=quoted_at)

    def __post_init__(self) -> None:
        if not isinstance(self.usd_per_xmr, Decimal):
            raise InvoiceValidationError("XMR/USD rate must use Decimal")
        if not self.usd_per_xmr.is_finite() or self.usd_per_xmr <= 0:
            raise InvoiceValidationError("XMR/USD rate must be finite and positive")
        if not isinstance(self.source, str) or not self.source.strip() or len(self.source) > 200:
            raise InvoiceValidationError("rate source must contain 1 through 200 characters")
        _require_aware_datetime(self.quoted_at, "quote timestamp")

    @property
    def rate_text(self) -> str:
        return format(self.usd_per_xmr, "f")


@dataclass(frozen=True)
class Invoice:
    id: str
    status_token: str = field(repr=False)
    service_id: str
    service_version: int
    service_name_snapshot: str
    service_description_snapshot: str
    duration_label_snapshot: str | None
    category_id: str
    category_name_snapshot: str
    category_description_snapshot: str
    price_usd_cents: int
    xmr_usd_rate: str
    rate_source: str
    quote_created_at: datetime
    expected_atomic: int
    observed_atomic: int
    xmr_address: str = field(repr=False)
    xmr_account_index: int
    xmr_address_index: int
    required_confirmations: int
    observed_confirmations: int
    deposit_txid: str | None = field(default=None, repr=False)
    sweep_txid: str | None = field(default=None, repr=False)
    sweep_required: bool = False
    status: PaymentStatus = PaymentStatus.AWAITING_PAYMENT
    status_note: str = STATUS_NOTES[PaymentStatus.AWAITING_PAYMENT]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc) + DEFAULT_INVOICE_TTL)
    expired_at: datetime | None = None
    settled_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _INVOICE_ID_PATTERN.fullmatch(self.id):
            raise InvoiceValidationError("invoice ID must be 32 lowercase hexadecimal characters")
        if not isinstance(self.status_token, str) or len(self.status_token) < 32:
            raise InvoiceValidationError("status token must contain at least 32 characters")
        for value, label, maximum in (
            (self.service_id, "service ID", 64),
            (self.service_name_snapshot, "service name snapshot", 200),
            (self.category_id, "category ID", 64),
            (self.category_name_snapshot, "category name snapshot", 200),
            (self.rate_source, "rate source", 200),
        ):
            _require_text(value, label, maximum=maximum)
        if not isinstance(self.service_description_snapshot, str):
            raise InvoiceValidationError("service description snapshot must be text")
        if not isinstance(self.category_description_snapshot, str):
            raise InvoiceValidationError("category description snapshot must be text")
        if self.duration_label_snapshot is not None:
            _require_text(self.duration_label_snapshot, "duration snapshot", maximum=200)
        for value, label in (
            (self.service_version, "service version"),
            (self.price_usd_cents, "USD price cents"),
            (self.expected_atomic, "expected atomic amount"),
            (self.required_confirmations, "required confirmations"),
        ):
            _require_positive_int(value, label)
        for value, label in (
            (self.observed_atomic, "observed atomic amount"),
            (self.xmr_account_index, "wallet account index"),
            (self.xmr_address_index, "wallet address index"),
            (self.observed_confirmations, "observed confirmations"),
        ):
            _require_non_negative_int(value, label)
        if self.expected_atomic > MAX_ATOMIC_UNITS or self.observed_atomic > MAX_ATOMIC_UNITS:
            raise InvoiceValidationError("atomic amount exceeds the supported storage range")
        if not isinstance(self.xmr_address, str) or not self.xmr_address:
            raise InvoiceValidationError("XMR address is required")
        try:
            rate = Decimal(self.xmr_usd_rate)
        except InvalidOperation as exc:
            raise InvoiceValidationError("stored XMR/USD rate is invalid") from exc
        if not rate.is_finite() or rate <= 0:
            raise InvoiceValidationError("stored XMR/USD rate must be finite and positive")
        if not isinstance(self.sweep_required, bool):
            raise InvoiceValidationError("sweep_required must be boolean")
        if not isinstance(self.status, PaymentStatus):
            raise InvoiceValidationError("invoice status is invalid")
        if self.status_note != STATUS_NOTES[self.status]:
            raise InvoiceValidationError("invoice status note does not match its status")
        for value, label in (
            (self.quote_created_at, "quote timestamp"),
            (self.created_at, "created_at"),
            (self.expires_at, "expires_at"),
            (self.updated_at, "updated_at"),
        ):
            _require_aware_datetime(value, label)
        if self.expires_at <= self.created_at:
            raise InvoiceValidationError("invoice expiry must follow creation")
        if self.updated_at < self.created_at:
            raise InvoiceValidationError("updated_at cannot precede creation")
        if (self.status is PaymentStatus.EXPIRED) != (self.expired_at is not None):
            raise InvoiceValidationError("expired_at must exist only for an expired invoice")
        if (self.status is PaymentStatus.SETTLED) != (self.settled_at is not None):
            raise InvoiceValidationError("settled_at must exist only for a settled invoice")

    @property
    def fully_paid_and_confirmed(self) -> bool:
        return (
            self.observed_atomic >= self.expected_atomic
            and self.observed_confirmations >= self.required_confirmations
        )


class InvoiceRepository(Protocol):
    def get_purchasable_service(self, service_id: str) -> PurchasableService | None: ...

    def insert_invoice(
        self, invoice: Invoice, expected_service: PurchasableService
    ) -> None: ...

    def insert_order_invoice(
        self, invoice: Invoice, lines: tuple[OrderLine, ...], *, customer_id: str,
        cart_version: int | None = None, claim_token: str | None = None,
        checkout_details: CheckoutDetails | None = None,
    ) -> None: ...


class SubaddressFactory(Protocol):
    def create_subaddress(self, label: str) -> XmrSubaddress: ...


class InvoiceCreator:
    """The application's single canonical XMR invoice-creation path."""

    def __init__(
        self,
        repository: InvoiceRepository,
        wallet: SubaddressFactory,
        *,
        required_confirmations: int,
        sweep_required: bool,
        invoice_ttl: timedelta = DEFAULT_INVOICE_TTL,
        maximum_quote_age: timedelta | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        invoice_id_factory: Callable[[], str] = lambda: secrets.token_hex(16),
        status_token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    ) -> None:
        _require_positive_int(required_confirmations, "required confirmations")
        if not isinstance(sweep_required, bool):
            raise InvoiceValidationError("sweep_required must be boolean")
        if not isinstance(invoice_ttl, timedelta) or invoice_ttl <= timedelta(0):
            raise InvoiceValidationError("invoice TTL must be positive")
        if maximum_quote_age is not None and (
            not isinstance(maximum_quote_age, timedelta)
            or maximum_quote_age <= timedelta(0)
        ):
            raise InvoiceValidationError("maximum quote age must be positive")
        self.repository = repository
        self.wallet = wallet
        self.required_confirmations = required_confirmations
        self.sweep_required = sweep_required
        self.invoice_ttl = invoice_ttl
        self.maximum_quote_age = maximum_quote_age
        self.now_factory = now_factory
        self.invoice_id_factory = invoice_id_factory
        self.status_token_factory = status_token_factory

    def create_invoice(
        self, service_id: str, quote: XmrQuote, *, customer_id: str | None = None
    ) -> Invoice:
        _require_text(service_id, "service ID", maximum=64)
        service = self.repository.get_purchasable_service(service_id)
        if service is None:
            raise ServiceUnavailableError("selected service is unavailable")
        return self._create_invoice((OrderLine(service, 1),), quote, customer_id=customer_id)

    def create_cart_invoice(
        self, customer_id: str, lines: tuple[OrderLine, ...], quote: XmrQuote,
        *, cart_version: int, claim_token: str, checkout_details: CheckoutDetails,
    ) -> Invoice:
        if not isinstance(customer_id, str) or not 16 <= len(customer_id) <= 64:
            raise InvoiceValidationError("customer ID is invalid")
        _require_positive_int(cart_version, "cart version")
        if not isinstance(claim_token, str) or not 16 <= len(claim_token) <= 200:
            raise InvoiceValidationError("cart checkout claim is invalid")
        if not isinstance(lines, tuple) or not lines or len(lines) > MAX_CART_SERVICES:
            raise InvoiceValidationError("cart size is invalid")
        for line in lines:
            if not isinstance(line, OrderLine):
                raise InvoiceValidationError("a validated order line is required")
            if self.repository.get_purchasable_service(line.service.service_id) != line.service:
                raise ServiceUnavailableError("a cart service changed")
        if not isinstance(checkout_details, CheckoutDetails):
            raise InvoiceValidationError("validated checkout details are required")
        checkout_details.require_services(tuple(line.service.service_id for line in lines))
        return self._create_invoice(
            lines, quote, customer_id=customer_id,
            cart_version=cart_version, claim_token=claim_token,
            checkout_details=checkout_details,
        )

    def _create_invoice(
        self, lines: tuple[OrderLine, ...], quote: XmrQuote,
        *, customer_id: str | None, cart_version: int | None = None,
        claim_token: str | None = None,
        checkout_details: CheckoutDetails | None = None,
    ) -> Invoice:
        if not isinstance(quote, XmrQuote):
            raise InvoiceValidationError("a validated XMR quote is required")
        if len({line.service.service_id for line in lines}) != len(lines):
            raise InvoiceValidationError("order service IDs must be unique")
        if customer_id is not None and (
            not isinstance(customer_id, str) or not 16 <= len(customer_id) <= 64
        ):
            raise InvoiceValidationError("customer ID is invalid")
        service = lines[0].service
        total_usd_cents = sum(line.total_usd_cents for line in lines)
        if total_usd_cents > 2**63 - 1:
            raise InvoiceValidationError("order total exceeds the storage range")
        bundled = len(lines) > 1 or lines[0].quantity > 1

        now = self.now_factory()
        _require_aware_datetime(now, "invoice creation time")
        if quote.quoted_at > now:
            raise InvoiceValidationError("quote timestamp cannot be in the future")
        if (
            self.maximum_quote_age is not None
            and now - quote.quoted_at > self.maximum_quote_age
        ):
            raise InvoiceValidationError("quote is older than the approved policy")

        expected_atomic = calculate_expected_atomic(
            total_usd_cents, quote.usd_per_xmr
        )
        invoice_id = self.invoice_id_factory()
        status_token = self.status_token_factory()
        if not isinstance(invoice_id, str) or not _INVOICE_ID_PATTERN.fullmatch(invoice_id):
            raise InvoiceValidationError(
                "generated invoice ID must be 32 lowercase hexadecimal characters"
            )
        if not isinstance(status_token, str) or len(status_token) < 32:
            raise InvoiceValidationError("generated status token is not strong enough")
        subaddress = self.wallet.create_subaddress(label=f"invoice:{invoice_id}")

        invoice = Invoice(
            id=invoice_id,
            status_token=status_token,
            service_id=service.service_id,
            service_version=service.service_version,
            service_name_snapshot=(
                f"Service order ({sum(line.quantity for line in lines)} items)"
                if bundled else service.service_name
            ),
            service_description_snapshot=(
                "See the saved order items for each service." if bundled else service.service_description
            ),
            duration_label_snapshot=None if bundled else service.duration_label,
            category_id=service.category_id,
            category_name_snapshot="Service order" if bundled else service.category_name,
            category_description_snapshot="" if bundled else service.category_description,
            price_usd_cents=total_usd_cents,
            xmr_usd_rate=quote.rate_text,
            rate_source=quote.source.strip(),
            quote_created_at=quote.quoted_at,
            expected_atomic=expected_atomic,
            observed_atomic=0,
            xmr_address=subaddress.address,
            xmr_account_index=subaddress.account_index,
            xmr_address_index=subaddress.address_index,
            required_confirmations=self.required_confirmations,
            observed_confirmations=0,
            sweep_required=self.sweep_required,
            created_at=now,
            expires_at=now + self.invoice_ttl,
            updated_at=now,
        )
        if customer_id is None:
            self.repository.insert_invoice(invoice, service)
        else:
            self.repository.insert_order_invoice(
                invoice, lines, customer_id=customer_id,
                cart_version=cart_version, claim_token=claim_token,
                checkout_details=checkout_details,
            )
        return invoice


def calculate_expected_atomic(price_usd_cents: int, usd_per_xmr: Decimal) -> int:
    _require_positive_int(price_usd_cents, "USD price cents")
    if not isinstance(usd_per_xmr, Decimal):
        raise InvoiceValidationError("XMR/USD rate must use Decimal")
    if not usd_per_xmr.is_finite() or usd_per_xmr <= 0:
        raise InvoiceValidationError("XMR/USD rate must be finite and positive")
    with localcontext() as context:
        context.prec = 50
        atomic_decimal = (
            Decimal(price_usd_cents) * ATOMIC_UNITS_PER_XMR
        ) / (Decimal(100) * usd_per_xmr)
        atomic = int(atomic_decimal.to_integral_value(rounding=ROUND_CEILING))
    if not 0 < atomic <= MAX_ATOMIC_UNITS:
        raise InvoiceValidationError("calculated atomic amount is outside the storage range")
    return atomic


def transition_invoice(
    invoice: Invoice, new_status: PaymentStatus, *, now: datetime
) -> Invoice:
    if not isinstance(new_status, PaymentStatus):
        raise IllegalInvoiceTransition("target payment status is invalid")
    _require_aware_datetime(now, "transition time")
    if now < invoice.updated_at:
        raise IllegalInvoiceTransition("transition time cannot move backwards")

    allowed = {
        PaymentStatus.AWAITING_PAYMENT: {
            PaymentStatus.PAID_PENDING_CONFIRMATIONS,
            PaymentStatus.EXPIRED,
        },
        PaymentStatus.PAID_PENDING_CONFIRMATIONS: {
            PaymentStatus.PAID_PENDING_SWEEP,
            PaymentStatus.SETTLED,
            PaymentStatus.EXPIRED,
        },
        PaymentStatus.PAID_PENDING_SWEEP: {PaymentStatus.SWEEPING_TO_COLD},
        PaymentStatus.SWEEPING_TO_COLD: {
            PaymentStatus.PAID_PENDING_SWEEP,
            PaymentStatus.SETTLED,
        },
        PaymentStatus.SETTLED: set(),
        PaymentStatus.EXPIRED: set(),
    }
    if new_status not in allowed[invoice.status]:
        raise IllegalInvoiceTransition(
            f"cannot transition {invoice.status.value} to {new_status.value}"
        )

    if new_status is PaymentStatus.EXPIRED:
        if now < invoice.expires_at:
            raise IllegalInvoiceTransition("invoice cannot expire before its expiry boundary")
        return replace(
            invoice,
            status=new_status,
            status_note=STATUS_NOTES[new_status],
            expired_at=now,
            updated_at=now,
        )

    if new_status is PaymentStatus.PAID_PENDING_CONFIRMATIONS and invoice.observed_atomic <= 0:
        raise IllegalInvoiceTransition("payment cannot be pending without an observed amount")
    if new_status in {
        PaymentStatus.PAID_PENDING_SWEEP,
        PaymentStatus.SWEEPING_TO_COLD,
        PaymentStatus.SETTLED,
    } and not invoice.fully_paid_and_confirmed:
        raise IllegalInvoiceTransition("invoice is not fully paid and confirmed")
    if new_status is PaymentStatus.PAID_PENDING_SWEEP and not invoice.sweep_required:
        raise IllegalInvoiceTransition("sweep-disabled invoice must not enter a sweep state")
    if new_status is PaymentStatus.SWEEPING_TO_COLD and not invoice.sweep_required:
        raise IllegalInvoiceTransition("sweep-disabled invoice must not enter a sweep state")
    if new_status is PaymentStatus.SETTLED:
        if invoice.sweep_required and not invoice.sweep_txid:
            raise IllegalInvoiceTransition("sweep-required invoice needs a recorded sweep transaction")
        if not invoice.sweep_required and invoice.status is not PaymentStatus.PAID_PENDING_CONFIRMATIONS:
            raise IllegalInvoiceTransition("sweep-disabled invoice settles after confirmations")

    return replace(
        invoice,
        status=new_status,
        status_note=STATUS_NOTES[new_status],
        settled_at=now if new_status is PaymentStatus.SETTLED else None,
        updated_at=now,
    )


def _require_text(value: str, label: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvoiceValidationError(f"{label} must contain 1 through {maximum} characters")


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvoiceValidationError(f"{label} must be a positive integer")


def _require_non_negative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvoiceValidationError(f"{label} must be a non-negative integer")


def _require_aware_datetime(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvoiceValidationError(f"{label} must be timezone-aware")
