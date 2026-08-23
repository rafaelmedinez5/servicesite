from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from app.catalog import CatalogValidationError, CategoryRecord, ServiceRecord
from app.payments.invoice import PaymentStatus
from app.payments.xmr_wallet_rpc import atomic_to_xmr_str
from app.persistence import (
    FulfillmentNotAllowedError,
    FulfillmentStatus,
    InvoiceNotFoundError,
    PersistenceError,
    ServicesiteRepository,
)
from app.web_security import FormSecurityError, require_csrf


admin = Blueprint("admin", __name__, url_prefix="/admin")
_ADMIN_SESSION_KEY = "_servicesite_admin"


@admin.before_request
def protect_admin_routes():
    g.no_store = True
    g.private_response = True
    if request.endpoint == "admin.login":
        return None
    candidate = session.get(_ADMIN_SESSION_KEY)
    username = current_app.config["ADMIN_USERNAME"]
    if not isinstance(candidate, str) or not secrets.compare_digest(candidate, username):
        return redirect(url_for("admin.login"), code=303)
    return None


@admin.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("admin/login.html")

    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        return render_template("admin/login.html", error="The form expired. Try again."), 400

    now = _now()
    repository = _repository()
    try:
        allowed = repository.admin_login_allowed(now=now)
    except (PersistenceError, sqlite3.Error):
        return render_template("admin/login.html", error="Login is temporarily unavailable."), 503
    if not allowed:
        return render_template(
            "admin/login.html",
            error="Too many attempts. Wait 15 minutes before trying again.",
        ), 429

    candidate_username = request.form.get("username", "")
    candidate_password = request.form.get("password", "")
    configured_username = current_app.config["ADMIN_USERNAME"]
    configured_hash = current_app.config["ADMIN_PASSWORD_HASH"]
    username_matches = (
        isinstance(candidate_username, str)
        and len(candidate_username) <= 64
        and secrets.compare_digest(candidate_username, configured_username)
    )
    password_matches = False
    if isinstance(candidate_password, str) and len(candidate_password) <= 1_024:
        try:
            password_matches = check_password_hash(configured_hash, candidate_password)
        except (ValueError, TypeError):
            password_matches = False

    if not (username_matches and password_matches):
        try:
            repository.record_admin_login_failure(now=now)
        except (PersistenceError, sqlite3.Error):
            return render_template("admin/login.html", error="Login is temporarily unavailable."), 503
        return render_template("admin/login.html", error="Invalid administrator credentials."), 401

    repository.clear_admin_login_failures()
    session.clear()
    session[_ADMIN_SESSION_KEY] = configured_username
    session.permanent = True
    return redirect(url_for("admin.dashboard"), code=303)


@admin.post("/logout")
def logout():
    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        abort(400)
    session.clear()
    return redirect(url_for("admin.login"), code=303)


@admin.get("")
def dashboard():
    try:
        purchases = _repository().list_admin_purchases(limit=500)
        categories = _repository().list_categories()
        services = _repository().list_services()
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template(
        "admin/dashboard.html",
        purchase_count=len(purchases),
        unsettled_count=sum(
            purchase.payment_status is not PaymentStatus.SETTLED for purchase in purchases
        ),
        fulfillment_count=sum(
            purchase.payment_status is PaymentStatus.SETTLED
            and purchase.fulfillment_status is FulfillmentStatus.UNFULFILLED
            for purchase in purchases
        ),
        category_count=sum(not item.archived for item in categories),
        service_count=sum(not item.service.archived for item in services),
    )


@admin.get("/categories")
def categories():
    try:
        records = _repository().list_categories()
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template("admin/categories.html", categories=records)


@admin.route("/categories/new", methods=["GET", "POST"])
def category_new():
    if request.method == "GET":
        return render_template("admin/category_form.html", category=None)
    return _save_category(None)


@admin.route("/categories/<category_id>/edit", methods=["GET", "POST"])
def category_edit(category_id: str):
    record = _repository().get_category(category_id)
    if record is None:
        abort(404)
    if request.method == "GET":
        return render_template("admin/category_form.html", category=record)
    return _save_category(record)


@admin.post("/categories/<category_id>/archive")
def category_archive(category_id: str):
    _require_admin_csrf()
    try:
        _repository().archive_category(category_id, now=_now())
    except (PersistenceError, sqlite3.Error):
        flash("The category could not be archived.", "error")
    else:
        flash("Category archived. Its services are no longer public.", "success")
    return redirect(url_for("admin.categories"), code=303)


@admin.get("/services")
def services():
    try:
        records = _repository().list_services()
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template("admin/services.html", services=records)


@admin.route("/services/new", methods=["GET", "POST"])
def service_new():
    categories = _repository().list_categories(include_archived=False)
    if request.method == "GET":
        return render_template(
            "admin/service_form.html", service=None, categories=categories
        )
    return _save_service(None, categories)


@admin.route("/services/<service_id>/edit", methods=["GET", "POST"])
def service_edit(service_id: str):
    record = _repository().get_service(service_id)
    if record is None:
        abort(404)
    categories = _repository().list_categories(include_archived=False)
    if request.method == "GET":
        return render_template(
            "admin/service_form.html", service=record, categories=categories
        )
    return _save_service(record, categories)


@admin.post("/services/<service_id>/archive")
def service_archive(service_id: str):
    _require_admin_csrf()
    try:
        _repository().archive_service(service_id, now=_now())
    except (PersistenceError, sqlite3.Error):
        flash("The service could not be archived.", "error")
    else:
        flash("Service archived and removed from the public catalog.", "success")
    return redirect(url_for("admin.services"), code=303)


@admin.get("/purchases")
def purchases():
    try:
        payment_status = _optional_enum(PaymentStatus, request.args.get("payment_status"))
        fulfillment_status = _optional_enum(
            FulfillmentStatus, request.args.get("fulfillment_status")
        )
        created_from, created_before = _date_range(
            request.args.get("date_from"), request.args.get("date_to")
        )
        records = _repository().list_admin_purchases(
            category_id=request.args.get("category_id") or None,
            service_id=request.args.get("service_id") or None,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
            created_from=created_from,
            created_before=created_before,
        )
        categories = _repository().list_categories()
        services = _repository().list_services()
    except (ValueError, PersistenceError, sqlite3.Error):
        flash("One or more purchase filters were invalid.", "error")
        return redirect(url_for("admin.purchases"), code=303)
    return render_template(
        "admin/purchases.html",
        purchases=records,
        categories=categories,
        services=services,
        payment_statuses=PaymentStatus,
        fulfillment_statuses=FulfillmentStatus,
        selected=request.args,
    )


@admin.get("/purchases/<invoice_id>")
def purchase_detail(invoice_id: str):
    try:
        purchase = _repository().get_admin_purchase(invoice_id)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    if purchase is None:
        abort(404)
    return render_template("admin/purchase_detail.html", purchase=purchase)


@admin.post("/purchases/<invoice_id>/fulfill")
def purchase_fulfill(invoice_id: str):
    _require_admin_csrf()
    try:
        _repository().mark_purchase_fulfilled(
            invoice_id, note=request.form.get("note", ""), now=_now()
        )
    except FulfillmentNotAllowedError:
        flash("Payment must be settled before fulfillment.", "error")
    except InvoiceNotFoundError:
        abort(404)
    except (PersistenceError, sqlite3.Error):
        flash("The fulfillment update could not be saved.", "error")
    else:
        flash("Purchase marked fulfilled.", "success")
    return redirect(url_for("admin.purchase_detail", invoice_id=invoice_id), code=303)


def register_admin(app) -> None:
    app.register_blueprint(admin)
    app.jinja_env.filters["atomic_xmr"] = atomic_to_xmr_str
    app.jinja_env.filters["usd_input"] = _usd_input


def _save_category(existing: CategoryRecord | None):
    _require_admin_csrf()
    now = _now()
    try:
        record = CategoryRecord(
            id=existing.id if existing else secrets.token_hex(16),
            name=request.form.get("name", ""),
            slug=request.form.get("slug", ""),
            description=request.form.get("description", ""),
            published=request.form.get("published") == "on",
            archived=existing.archived if existing else False,
            sort_order=_sort_order(request.form.get("sort_order", "0")),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        if existing:
            _repository().update_category(record)
        else:
            _repository().insert_category(record)
    except (CatalogValidationError, PersistenceError, sqlite3.Error, ValueError) as exc:
        return render_template(
            "admin/category_form.html",
            category=existing,
            error=_safe_form_error(exc),
        ), 400
    flash("Category saved.", "success")
    return redirect(url_for("admin.categories"), code=303)


def _save_service(existing: ServiceRecord | None, categories: list[CategoryRecord]):
    _require_admin_csrf()
    now = _now()
    try:
        duration = request.form.get("duration_label", "").strip() or None
        record = ServiceRecord(
            id=existing.id if existing else secrets.token_hex(16),
            category_id=request.form.get("category_id", ""),
            name=request.form.get("name", ""),
            slug=request.form.get("slug", ""),
            description=request.form.get("description", ""),
            price_usd_cents=_usd_cents(request.form.get("price_usd", "")),
            duration_label=duration,
            published=request.form.get("published") == "on",
            archived=existing.archived if existing else False,
            sort_order=_sort_order(request.form.get("sort_order", "0")),
            version=existing.version if existing else 1,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        if existing:
            _repository().update_service(record)
        else:
            _repository().insert_service(record)
    except (CatalogValidationError, PersistenceError, sqlite3.Error, ValueError) as exc:
        return render_template(
            "admin/service_form.html",
            service=existing,
            categories=categories,
            error=_safe_form_error(exc),
        ), 400
    flash("Service saved.", "success")
    return redirect(url_for("admin.services"), code=303)


def _repository() -> ServicesiteRepository:
    return current_app.extensions["servicesite_repository"]


def _now() -> datetime:
    factory: Callable[[], datetime] = current_app.extensions.get(
        "servicesite_now_factory", lambda: datetime.now(timezone.utc)
    )
    return factory()


def _require_admin_csrf() -> None:
    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        abort(400)


def _sort_order(raw: str) -> int:
    value = int(raw)
    if not -1_000_000 <= value <= 1_000_000:
        raise ValueError("sort order is out of range")
    return value


def _usd_cents(raw: str) -> int:
    try:
        amount = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("USD price is invalid") from exc
    cents = amount * 100
    if not amount.is_finite() or amount <= 0 or cents != cents.to_integral_value():
        raise ValueError("USD price must be positive with at most two decimal places")
    value = int(cents)
    if value > 9_223_372_036_854_775_807:
        raise ValueError("USD price is too large")
    return value


def _usd_input(cents: int) -> str:
    dollars, remainder = divmod(cents, 100)
    return f"{dollars}.{remainder:02d}"


def _optional_enum(enum_type, raw_value):
    if not raw_value:
        return None
    return enum_type(raw_value)


def _date_range(raw_from: str | None, raw_to: str | None) -> tuple[datetime | None, datetime | None]:
    start = (
        datetime.combine(date.fromisoformat(raw_from), time.min, tzinfo=timezone.utc)
        if raw_from
        else None
    )
    before = (
        datetime.combine(date.fromisoformat(raw_to), time.min, tzinfo=timezone.utc)
        + timedelta(days=1)
        if raw_to
        else None
    )
    if start is not None and before is not None and start >= before:
        raise ValueError("date range is invalid")
    return start, before


def _safe_form_error(exc: Exception) -> str:
    if isinstance(exc, (CatalogValidationError, ValueError)):
        return str(exc)
    return "The record could not be saved. Check for a duplicate slug."
