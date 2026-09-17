from __future__ import annotations

import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.academy import ACADEMY_ENROLLMENT_FEE_CENTS, ACADEMY_TIERS, get_academy_tier
from app.payments.invoice import InvoiceError
from app.payments.xmr_rate import XmrRateError
from app.payments.xmr_wallet_rpc import XmrWalletRpcError
from app.persistence import (
    AcademyEnrollmentExistsError,
    ActiveOrderExistsError,
    PersistenceError,
)
from app.web import _invoice_creator, _rate_provider, _repository
from app.web_security import (
    FormSecurityError,
    consume_checkout_nonce,
    issue_checkout_nonce,
    require_csrf,
)


academy = Blueprint("academy", __name__)


@academy.get("/academy")
def overview():
    g.no_store = True
    enrollment = None
    if g.customer is not None:
        try:
            enrollment = _repository().get_academy_enrollment(g.customer.id)
        except (PersistenceError, sqlite3.Error):
            abort(503)
    return render_template(
        "academy.html",
        tiers=ACADEMY_TIERS,
        enrollment_fee_usd_cents=ACADEMY_ENROLLMENT_FEE_CENTS,
        enrollment=enrollment,
        enrolled_tier=(
            get_academy_tier(enrollment.tier_key) if enrollment is not None else None
        ),
        checkout_nonce=issue_checkout_nonce() if g.customer is not None and enrollment is None else None,
    )


@academy.post("/academy/enroll")
def enroll():
    g.no_store = True
    g.private_response = True
    if g.customer is None:
        return redirect(url_for("customer.login", next=url_for("academy.overview")), code=303)
    try:
        require_csrf(request.form.get("csrf_token"))
        consume_checkout_nonce(request.form.get("checkout_nonce"))
    except FormSecurityError:
        return _academy_error("The enrollment form expired. Review the program and try again.", 400)

    tier = get_academy_tier(request.form.get("tier", ""))
    if tier is None:
        return _academy_error("Choose a valid Academy tier.", 400)

    repository = _repository()
    try:
        existing = repository.get_academy_enrollment(g.customer.id)
        if existing is not None:
            flash("Your Academy enrollment has already been recorded.", "error")
            return redirect(url_for("academy.overview"), code=303)
        active_order = repository.get_active_customer_order(g.customer.id)
        if active_order is not None:
            flash("Finish or cancel your current order before enrolling.", "error")
            return redirect(
                url_for("shopping.order_detail", invoice_id=active_order.id), code=303
            )
        service = repository.get_academy_enrollment_service()
        quote = _rate_provider().get_quote()
        invoice = _invoice_creator().create_academy_enrollment_invoice(
            service,
            quote,
            customer_id=g.customer.id,
            tier_key=tier.key,
            tuition_usd_cents=tier.tuition_usd_cents,
        )
    except AcademyEnrollmentExistsError:
        flash("Your Academy enrollment has already been recorded.", "error")
        return redirect(url_for("academy.overview"), code=303)
    except ActiveOrderExistsError as exc:
        flash("Finish or cancel your current order before enrolling.", "error")
        return redirect(url_for("shopping.order_detail", invoice_id=exc.invoice_id), code=303)
    except (XmrRateError, XmrWalletRpcError, InvoiceError, PersistenceError, sqlite3.Error):
        return _academy_error(
            "Enrollment payment could not be started. No payment is required.", 503
        )

    return redirect(
        url_for("public.checkout", invoice_id=invoice.id, status_token=invoice.status_token),
        code=303,
    )


def _academy_error(message: str, status_code: int):
    return render_template(
        "academy.html",
        tiers=ACADEMY_TIERS,
        enrollment_fee_usd_cents=ACADEMY_ENROLLMENT_FEE_CENTS,
        enrollment=None,
        enrolled_tier=None,
        checkout_nonce=issue_checkout_nonce() if g.customer is not None else None,
        error=message,
    ), status_code


def register_academy(app) -> None:
    app.register_blueprint(academy)
