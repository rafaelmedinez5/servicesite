from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from app.checkout_details import CheckoutValidationError, parse_checkout_details
from app.orders import CartChangedError, CartError, CheckoutInProgressError
from app.payments.invoice import InvoiceError, PaymentStatus, ServiceUnavailableError
from app.payments.xmr_rate import XmrRateError
from app.payments.xmr_wallet_rpc import XmrWalletRpcError, atomic_to_xmr_str
from app.persistence import CatalogChangedError, FulfillmentStatus, PersistenceError
from app.web import _invoice_creator, _rate_provider, customer_payment_state
from app.web_security import FormSecurityError, consume_checkout_nonce, issue_checkout_nonce, require_csrf


shopping = Blueprint("shopping", __name__)


@shopping.before_request
def protect_shopping():
    g.no_store = True
    g.private_response = True
    if g.customer is None:
        return redirect(url_for(
            "customer.login", next=request.path if request.method == "GET" else url_for("shopping.cart")
        ), code=303)
    if request.method == "POST":
        try:
            require_csrf(request.form.get("csrf_token"))
        except FormSecurityError:
            abort(400)
    return None


@shopping.errorhandler(PersistenceError)
@shopping.errorhandler(sqlite3.Error)
def shopping_unavailable(_error):
    return render_template(
        "error.html", title="Shopping temporarily unavailable",
        message="Your saved cart and orders cannot be loaded right now. Please try again later.",
        private_page=True,
    ), 503


@shopping.get("/cart")
def cart():
    return _render_cart()


@shopping.post("/cart/add")
def add_item():
    try:
        _repository().change_cart_item(
            g.customer.id, request.form.get("service_id", ""),
            _quantity(request.form.get("quantity", "1")), add=True,
        )
    except CartError as exc:
        return _render_cart(error=str(exc), status_code=400)
    flash("Service added to your cart.", "success")
    return redirect(url_for("shopping.cart"), code=303)


@shopping.post("/cart/items/<service_id>")
def update_item(service_id: str):
    try:
        quantity = _quantity(request.form.get("quantity", ""))
        _repository().change_cart_item(g.customer.id, service_id, quantity)
    except CartError as exc:
        return _render_cart(error=str(exc), status_code=400)
    flash("Item removed." if quantity == 0 else "Cart updated.", "success")
    return redirect(url_for("shopping.cart"), code=303)


@shopping.get("/cart/checkout")
def checkout_review():
    if not _repository().get_cart(g.customer.id).ready:
        return redirect(url_for("shopping.cart"), code=303)
    return _render_checkout_review()


@shopping.post("/cart/checkout")
def checkout_cart():
    try:
        consume_checkout_nonce(request.form.get("checkout_nonce"))
        raw_version = request.form.get("cart_version", "")
        if not raw_version.isascii() or not raw_version.isdecimal() or len(raw_version) > 18:
            raise FormSecurityError("invalid cart revision")
        version = int(raw_version)
    except FormSecurityError:
        return _render_checkout_review(error="This checkout form expired. Review the cart and try again.", status_code=400)

    repository = _repository()
    claim_token = secrets.token_urlsafe(24)
    claimed = False
    completed = False
    try:
        lines = repository.claim_cart_checkout(
            g.customer.id, version=version, fingerprint=request.form.get("cart_fingerprint", ""),
            claim_token=claim_token, now=_now(),
        )
        claimed = True
        details = parse_checkout_details(request.form, tuple(line.service.service_id for line in lines))
        quote = _rate_provider().get_quote()
        invoice = _invoice_creator().create_cart_invoice(
            g.customer.id, lines, quote, cart_version=version, claim_token=claim_token,
            checkout_details=details,
        )
        completed = True
    except CheckoutValidationError as exc:
        return _render_checkout_review(error=str(exc), errors=exc.errors, status_code=400)
    except (CartChangedError, CheckoutInProgressError, CatalogChangedError, ServiceUnavailableError):
        return _render_checkout_review(
            error="The cart changed or checkout is already in progress. Review the current items and your orders before trying again.",
            status_code=409,
        )
    except (XmrRateError, XmrWalletRpcError, InvoiceError, PersistenceError, sqlite3.Error):
        return _render_checkout_review(
            error="An invoice could not be created. Your cart is still saved; no payment is required.",
            status_code=503,
        )
    finally:
        if claimed and not completed:
            try:
                repository.release_cart_checkout(g.customer.id, claim_token)
            except (PersistenceError, sqlite3.Error):
                pass  # A five-minute lease bounds recovery without exposing database errors.
    return redirect(url_for(
        "public.checkout", invoice_id=invoice.id, status_token=invoice.status_token
    ), code=303)


@shopping.get("/account/orders/<invoice_id>")
def order_detail(invoice_id: str):
    repository = _repository()
    invoice = repository.get_customer_order(g.customer.id, invoice_id)
    if invoice is None:
        abort(404)
    purchase = repository.get_admin_purchase(invoice.id)
    return render_template(
        "customer/order.html", invoice=invoice, items=repository.get_invoice_items(invoice.id),
        checkout_details=repository.get_order_checkout_details(invoice.id),
        account_delivery=repository.get_account_delivery(invoice.id),
        xmr_amount=atomic_to_xmr_str(invoice.expected_atomic),
        customer_state=customer_payment_state(
            invoice, fulfilled=purchase.fulfillment_status is FulfillmentStatus.FULFILLED
        ),
    )


def register_shopping(app) -> None:
    app.register_blueprint(shopping)

    @app.template_filter("customer_payment_label")
    def payment_label(status: PaymentStatus) -> str:
        return {
            PaymentStatus.AWAITING_PAYMENT: "Awaiting payment",
            PaymentStatus.PAID_PENDING_CONFIRMATIONS: "Payment detected",
            PaymentStatus.PAID_PENDING_SWEEP: "Payment confirmed",
            PaymentStatus.SWEEPING_TO_COLD: "Payment confirmed",
            PaymentStatus.SETTLED: "Payment settled",
            PaymentStatus.EXPIRED: "Payment window expired",
        }[status]

    @app.context_processor
    def cart_badge():
        if getattr(g, "customer", None) is None:
            return {"cart_quantity": 0}
        try:
            quantity = _repository().get_cart(g.customer.id).quantity
        except (PersistenceError, sqlite3.Error):
            quantity = None
        return {"cart_quantity": quantity}


def _render_cart(*, error: str | None = None, status_code: int = 200):
    snapshot = _repository().get_cart(g.customer.id)
    return render_template(
        "cart.html", cart=snapshot, error=error,
    ), status_code


def _render_checkout_review(*, error: str | None = None, errors=None, status_code: int = 200):
    snapshot = _repository().get_cart(g.customer.id)
    if not snapshot.ready:
        return _render_cart(error=error, status_code=status_code)
    return render_template(
        "checkout_review.html", cart=snapshot, error=error, errors=errors or {},
        values=request.form if request.method == "POST" else {},
        checkout_nonce=issue_checkout_nonce(),
    ), status_code


def _quantity(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 2:
        raise CartError("Use a whole-number quantity from 0 to 10.")
    return int(value)


def _repository():
    return current_app.extensions["servicesite_repository"]


def _now() -> datetime:
    factory = current_app.extensions.get("servicesite_now_factory", lambda: datetime.now(timezone.utc))
    return factory()
