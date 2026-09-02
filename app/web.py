from __future__ import annotations

import io
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import quote, urlencode

import segno
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app.catalog import CategoryRecord, PurchasableService
from app.orders import CartError
from app.payments.invoice import (
    Invoice,
    InvoiceCreator,
    PaymentStatus,
    ServiceUnavailableError,
)
from app.payments.xmr_rate import (
    CoinGeckoRateClient,
    CoinGeckoRateConfig,
)
from app.payments.xmr_wallet_rpc import (
    WalletRpcConfig,
    XmrWalletRpcClient,
    atomic_to_xmr_str,
)
from app.persistence import FulfillmentStatus, PersistenceError, ServicesiteRepository
from app.service_images import ServiceImageError, ServiceImageStore
from app.web_security import (
    FormSecurityError,
    consume_checkout_nonce,
    csrf_token,
    issue_checkout_nonce,
    require_csrf,
)


public = Blueprint("public", __name__)
PUBLIC_PGP_KEY = Path(__file__).with_name("public_key.asc").read_text(encoding="ascii")


class RateProvider(Protocol):
    def get_quote(self): ...


@dataclass(frozen=True)
class PublicCategory:
    id: str
    name: str
    slug: str
    description: str
    services: tuple[PurchasableService, ...]


@dataclass(frozen=True)
class CustomerPaymentState:
    title: str
    message: str
    tone: str
    confirmation_text: str | None
    fulfillment_text: str


@public.get("/")
def index():
    g.no_store = True
    try:
        category_records = _published_category_records()
        services = _repository().list_purchasable_services()
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Catalog temporarily unavailable",
            "The service catalog cannot be loaded right now. Please try again later.",
            503,
        )
    return render_template(
        "index.html",
        categories=_group_services(category_records, services),
    )


@public.get("/categories/<category_slug>")
def category_detail(category_slug: str):
    g.no_store = True
    if not category_slug or len(category_slug) > 120:
        abort(404)
    try:
        category = next(
            (
                item
                for item in _published_category_records()
                if item.slug == category_slug
            ),
            None,
        )
        services = _repository().list_purchasable_services()
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Category temporarily unavailable",
            "This service category cannot be loaded right now. Please try again later.",
            503,
        )
    if category is None:
        abort(404)
    return render_template(
        "category_detail.html",
        category=category,
        services=tuple(
            service for service in services if service.category_id == category.id
        ),
    )


@public.get("/about")
def about():
    g.no_store = True
    return render_template("about.html")


@public.get("/join")
def join():
    g.no_store = True
    return render_template("join.html")


@public.get("/contact")
def contact():
    g.no_store = True
    return render_template(
        "contact.html",
        contact_method=current_app.config.get("PUBLIC_CONTACT_METHOD"),
        contact_address=current_app.config.get("PUBLIC_CONTACT_ADDRESS"),
    )


@public.get("/pgp-key")
def pgp_key():
    g.no_store = True
    return Response(
        PUBLIC_PGP_KEY,
        content_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'inline; filename="servicesite-public-key.asc"'
        },
    )


@public.get("/services/<service_slug>")
def service_detail(service_slug: str):
    g.no_store = True
    if not service_slug or len(service_slug) > 120:
        abort(404)
    try:
        service = _repository().get_purchasable_service_by_slug(service_slug)
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Service temporarily unavailable",
            "This service cannot be loaded right now. Please try again later.",
            503,
        )
    if service is None:
        abort(404)
    try:
        category = next(
            (
                item
                for item in _published_category_records()
                if item.id == service.category_id
            ),
            None,
        )
        related_services = _related_services(
            service, _repository().list_purchasable_services()
        )
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Service temporarily unavailable",
            "Recommendations cannot be loaded right now. Please try again later.",
            503,
        )
    if category is None:
        abort(404)
    return render_template(
        "service_detail.html",
        service=service,
        category_slug=category.slug,
        related_services=related_services,
        checkout_nonce=issue_checkout_nonce() if g.customer is not None else None,
    )


@public.get("/services/<service_slug>/image/<image_key>.webp")
def service_image(service_slug: str, image_key: str):
    if not service_slug or len(service_slug) > 120:
        abort(404)
    try:
        service = _repository().get_purchasable_service_by_slug(service_slug)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    if (
        service is None
        or service.image_key is None
        or not secrets.compare_digest(service.image_key, image_key)
    ):
        abort(404)
    try:
        data = _image_store().read(image_key)
    except ServiceImageError:
        abort(404)
    if data is None:
        abort(404)

    response = Response(
        data,
        200,
        {
            "Content-Type": "image/webp",
            "Content-Disposition": 'inline; filename="service-image.webp"',
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
    response.set_etag(image_key)
    return response.make_conditional(request)


@public.post("/checkout")
def create_checkout():
    _mark_private()
    if g.customer is None:
        return redirect(
            url_for("customer.login", next=url_for("public.index")), code=303
        )
    try:
        require_csrf(request.form.get("csrf_token"))
        service_id = request.form.get("service_id", "")
        if not isinstance(service_id, str) or not service_id.strip() or len(service_id) > 64:
            raise FormSecurityError("invalid service selection")
        selected_service = _repository().get_purchasable_service(service_id)
        if selected_service is None:
            raise ServiceUnavailableError("selected service is unavailable")
        consume_checkout_nonce(request.form.get("checkout_nonce"))
    except FormSecurityError:
        return _error_page(
            "Request could not be verified",
            "Return to the service page and submit a new checkout request.",
            400,
        )
    except ServiceUnavailableError:
        return _error_page(
            "Service unavailable",
            "That service is not currently available for purchase.",
            400,
        )
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Checkout temporarily unavailable",
            "A payment invoice could not be created. No payment is required.",
            503,
        )

    try:
        _repository().change_cart_item(
            g.customer.id, selected_service.service_id, 1, ensure_present=True
        )
    except CartError:
        return _error_page(
            "Review your cart",
            "This service could not be added. Review your cart for unavailable items or its item limit.",
            409,
        )
    except (PersistenceError, sqlite3.Error):
        return _error_page(
            "Checkout temporarily unavailable",
            "A payment invoice could not be created. No payment is required.",
            503,
        )

    return redirect(url_for("shopping.checkout_review"), code=303)


@public.get("/checkout/<invoice_id>/<status_token>")
def checkout(invoice_id: str, status_token: str):
    invoice = _private_invoice(invoice_id, status_token)
    amount = atomic_to_xmr_str(invoice.expected_atomic)
    try:
        items = _repository().get_invoice_items(invoice.id)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template(
        "checkout.html",
        invoice=invoice,
        xmr_amount=amount,
        monero_uri=build_monero_uri(invoice),
        items=items,
    )


@public.get("/checkout/<invoice_id>/<status_token>/qr.png")
def checkout_qr(invoice_id: str, status_token: str):
    invoice = _private_invoice(invoice_id, status_token)
    qr = segno.make(build_monero_uri(invoice), micro=False, error="m")
    output = io.BytesIO()
    qr.save(output, kind="png", scale=5, border=2, dark="#000", light="#fff")
    return Response(
        output.getvalue(),
        200,
        {
            "Content-Type": "image/png",
            "Content-Disposition": 'inline; filename="monero-payment.png"',
        },
    )


@public.get("/status/<invoice_id>/<status_token>")
def status(invoice_id: str, status_token: str):
    invoice = _private_invoice(invoice_id, status_token)
    try:
        purchase = _repository().get_admin_purchase(invoice.id)
        items = _repository().get_invoice_items(invoice.id)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template(
        "status.html",
        invoice=invoice,
        items=items,
        xmr_amount=atomic_to_xmr_str(invoice.expected_atomic),
        customer_state=customer_payment_state(
            invoice,
            fulfilled=(
                purchase is not None
                and purchase.fulfillment_status is FulfillmentStatus.FULFILLED
            ),
        ),
    )


def register_web(app) -> None:
    app.register_blueprint(public)
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["usd"] = _format_usd
    app.jinja_env.filters["display_datetime"] = _format_datetime

    @app.context_processor
    def inject_public_navigation():
        if not getattr(g, "site_authenticated", False):
            categories = ()
        else:
            try:
                categories = _published_category_records()
            except (PersistenceError, sqlite3.Error):
                categories = ()
        return {
            "navigation_categories": categories,
            "public_onion_address": current_app.config.get(
                "PUBLIC_ONION_ADDRESS"
            ),
        }

    @app.after_request
    def apply_security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; script-src 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        if getattr(g, "no_store", False):
            response.headers["Cache-Control"] = "no-store, private, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Vary"] = "Cookie"
        if getattr(g, "private_response", False):
            response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return _error_page(
            "Page not found",
            "The requested page is unavailable or the private link is invalid.",
            404,
        )

    @app.errorhandler(413)
    def request_too_large(_error):
        return _error_page(
            "Request too large",
            "The submitted file or form exceeds the allowed size.",
            413,
        )


def build_monero_uri(invoice: Invoice) -> str:
    amount = atomic_to_xmr_str(invoice.expected_atomic)
    return f"monero:{quote(invoice.xmr_address, safe='')}?{urlencode({'tx_amount': amount})}"


def customer_payment_state(
    invoice: Invoice, *, fulfilled: bool = False
) -> CustomerPaymentState:
    not_fulfilled = "Service fulfillment has not started."
    if invoice.status is PaymentStatus.AWAITING_PAYMENT and invoice.observed_atomic > 0:
        remaining = max(invoice.expected_atomic - invoice.observed_atomic, 0)
        return CustomerPaymentState(
            title="Partial payment received",
            message=(
                f"The invoice still requires {atomic_to_xmr_str(remaining)} XMR. "
                "Do not send funds after the expiry time."
            ),
            tone="warning",
            confirmation_text=None,
            fulfillment_text=not_fulfilled,
        )
    if invoice.status is PaymentStatus.AWAITING_PAYMENT:
        return CustomerPaymentState(
            title="Awaiting payment",
            message="No qualifying payment has been detected for this invoice.",
            tone="waiting",
            confirmation_text=None,
            fulfillment_text=not_fulfilled,
        )
    if invoice.status is PaymentStatus.PAID_PENDING_CONFIRMATIONS:
        return CustomerPaymentState(
            title="Payment detected",
            message="The payment is waiting for the required Monero confirmations.",
            tone="waiting",
            confirmation_text=(
                f"{min(invoice.observed_confirmations, invoice.required_confirmations)} "
                f"of {invoice.required_confirmations} confirmations"
            ),
            fulfillment_text=not_fulfilled,
        )
    if invoice.status in {
        PaymentStatus.PAID_PENDING_SWEEP,
        PaymentStatus.SWEEPING_TO_COLD,
    }:
        return CustomerPaymentState(
            title="Payment confirmed",
            message="The payment is being finalized. No additional payment is required.",
            tone="waiting",
            confirmation_text=(
                f"{invoice.required_confirmations} of {invoice.required_confirmations} confirmations"
            ),
            fulfillment_text=not_fulfilled,
        )
    if invoice.status is PaymentStatus.SETTLED:
        return CustomerPaymentState(
            title="Payment settled",
            message="The required payment lifecycle is complete.",
            tone="success",
            confirmation_text=(
                f"{invoice.required_confirmations} of {invoice.required_confirmations} confirmations"
            ),
            fulfillment_text=(
                "Service fulfillment has been marked complete."
                if fulfilled
                else "The purchase is eligible for manual fulfillment review."
            ),
        )
    return CustomerPaymentState(
        title="Payment window expired",
        message="This invoice can no longer accept a new payment.",
        tone="expired",
        confirmation_text=None,
        fulfillment_text=not_fulfilled,
    )


def _private_invoice(invoice_id: str, status_token: str) -> Invoice:
    _mark_private()
    try:
        invoice = _repository().get_invoice_by_token(invoice_id, status_token)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    if invoice is None:
        abort(404)
    return invoice


def _mark_private() -> None:
    g.no_store = True
    g.private_response = True


def _repository() -> ServicesiteRepository:
    return current_app.extensions["servicesite_repository"]


def _image_store() -> ServiceImageStore:
    return current_app.extensions["servicesite_image_store"]


def _rate_provider() -> RateProvider:
    injected = current_app.extensions.get("servicesite_rate_provider")
    if injected is not None:
        return injected
    return CoinGeckoRateClient(
        CoinGeckoRateConfig(
            api_key=current_app.config["COINGECKO_API_KEY"],
            timeout_seconds=current_app.config["XMR_RATE_TIMEOUT"],
            maximum_age=timedelta(
                seconds=current_app.config["XMR_QUOTE_MAX_AGE_SECONDS"]
            ),
        )
    )


def _invoice_creator() -> InvoiceCreator:
    wallet = current_app.extensions.get("servicesite_wallet_client")
    if wallet is None:
        wallet = XmrWalletRpcClient(
            WalletRpcConfig(
                url=current_app.config["XMR_WALLET_RPC_URL"],
                username=current_app.config["XMR_WALLET_RPC_USER"],
                password=current_app.config["XMR_WALLET_RPC_PASS"],
                account_index=current_app.config["XMR_ACCOUNT_INDEX"],
                timeout_seconds=current_app.config["XMR_RPC_TIMEOUT"],
                max_attempts=current_app.config["XMR_RPC_RETRIES"],
                retry_backoff_seconds=current_app.config["XMR_RPC_RETRY_BACKOFF"],
            )
        )
    now_factory: Callable[[], datetime] = current_app.extensions.get(
        "servicesite_now_factory", lambda: datetime.now(timezone.utc)
    )
    return InvoiceCreator(
        _repository(),
        wallet,
        required_confirmations=current_app.config["XMR_MIN_CONFIRMATIONS"],
        sweep_required=current_app.config["XMR_SWEEP_ENABLED"],
        invoice_ttl=timedelta(seconds=current_app.config["XMR_INVOICE_TTL_SECONDS"]),
        maximum_quote_age=timedelta(
            seconds=current_app.config["XMR_QUOTE_MAX_AGE_SECONDS"]
        ),
        now_factory=now_factory,
    )


def _published_category_records() -> tuple[CategoryRecord, ...]:
    return tuple(
        category
        for category in _repository().list_categories(include_archived=False)
        if category.published and not category.archived
    )


def _group_services(
    categories: tuple[CategoryRecord, ...], services: list[PurchasableService]
) -> tuple[PublicCategory, ...]:
    services_by_category: dict[str, list[PurchasableService]] = {}
    for service in services:
        services_by_category.setdefault(service.category_id, []).append(service)
    return tuple(
        PublicCategory(
            id=category.id,
            name=category.name,
            slug=category.slug,
            description=category.description,
            services=tuple(services_by_category.get(category.id, ())),
        )
        for category in categories
    )


def _related_services(
    selected: PurchasableService,
    services: list[PurchasableService],
    *,
    limit: int = 3,
) -> tuple[PurchasableService, ...]:
    candidates = [
        service
        for service in services
        if service.service_id != selected.service_id
        and service.category_id == selected.category_id
    ]
    candidates.extend(
        service
        for service in services
        if service.service_id != selected.service_id
        and service.category_id != selected.category_id
    )
    return tuple(candidates[:limit])


def _format_usd(price_usd_cents: int) -> str:
    dollars, cents = divmod(price_usd_cents, 100)
    return f"${dollars:,}.{cents:02d} USD"


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _error_page(title: str, message: str, status_code: int):
    return render_template("error.html", title=title, message=message), status_code
