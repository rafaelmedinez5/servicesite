from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

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
from werkzeug.security import check_password_hash, generate_password_hash

from app.persistence import CustomerAccount, PersistenceError, ServicesiteRepository
from app.web_security import FormSecurityError, require_csrf


customer = Blueprint("customer", __name__)
_CUSTOMER_SESSION_KEY = "_servicesite_customer"
_USERNAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,30}[a-z0-9])")
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32))


@customer.before_app_request
def load_customer() -> None:
    g.customer = None
    candidate = session.get(_CUSTOMER_SESSION_KEY)
    if not isinstance(candidate, dict):
        return
    customer_id = candidate.get("id")
    username = candidate.get("username")
    credential_version = candidate.get("credential_version")
    if not isinstance(customer_id, str) or not isinstance(username, str):
        session.pop(_CUSTOMER_SESSION_KEY, None)
        return
    try:
        account = _repository().get_customer_account_by_id(customer_id)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    if (
        account is None
        or not secrets.compare_digest(account.username, username)
        or account.credential_version != credential_version
    ):
        session.pop(_CUSTOMER_SESSION_KEY, None)
        return
    g.customer = account


@customer.before_request
def mark_customer_pages_private() -> None:
    g.no_store = True
    g.private_response = True


@customer.route("/register", methods=["GET", "POST"])
def register():
    if g.customer is not None:
        return redirect(url_for("customer.account"), code=303)
    next_path = _safe_next(request.values.get("next"))
    if request.method == "GET":
        return render_template("customer/register.html", next_path=next_path)

    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        return _registration_response(
            error="The form expired. Try again.",
            status_code=400,
            next_path=next_path,
        )

    raw_username = request.form.get("username", "")
    username = _normalize_username(raw_username)
    password = request.form.get("password", "")
    confirmation = request.form.get("confirm_password", "")
    error = _registration_error(username, password, confirmation)
    if error is not None:
        return _registration_response(
            error=error,
            status_code=400,
            username=username or "",
            next_path=next_path,
        )

    try:
        account = _repository().create_customer_account(
            customer_id=secrets.token_urlsafe(18),
            username=username,
            password_hash=generate_password_hash(password),
            now=_now(),
        )
    except (PersistenceError, sqlite3.Error, ValueError):
        return _registration_response(
            error="Account creation is temporarily unavailable.",
            status_code=503,
            username=username,
            next_path=next_path,
        )
    if account is None:
        return _registration_response(
            error="That username is unavailable.",
            status_code=409,
            username=username,
            next_path=next_path,
        )

    _start_customer_session(account)
    flash("Your account has been created.", "success")
    return redirect(next_path or url_for("customer.account"), code=303)


@customer.route("/login", methods=["GET", "POST"])
def login():
    if g.customer is not None:
        return redirect(url_for("customer.account"), code=303)
    next_path = _safe_next(request.values.get("next"))
    if request.method == "GET":
        return render_template("customer/login.html", next_path=next_path)

    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        return _login_response(
            error="The form expired. Try again.",
            status_code=400,
            next_path=next_path,
        )

    raw_username = request.form.get("username", "")
    username = _normalize_username(raw_username)
    candidate_password = request.form.get("password", "")
    repository = _repository()
    try:
        account = (
            repository.get_customer_account_by_username(username)
            if username is not None
            else None
        )
    except (PersistenceError, sqlite3.Error):
        return _login_response(
            error="Login is temporarily unavailable.",
            status_code=503,
            username=username or "",
            next_path=next_path,
        )

    if account is None:
        _password_matches(_DUMMY_PASSWORD_HASH, candidate_password)
        return _login_response(
            error="Invalid username or password.",
            status_code=401,
            username=username or "",
            next_path=next_path,
        )

    now = _now()
    try:
        allowed = repository.customer_login_allowed(account.id, now=now)
    except (PersistenceError, sqlite3.Error):
        return _login_response(
            error="Login is temporarily unavailable.",
            status_code=503,
            username=account.username,
            next_path=next_path,
        )
    if not allowed:
        return _login_response(
            error="Too many attempts. Wait 15 minutes before trying again.",
            status_code=429,
            username=account.username,
            next_path=next_path,
        )

    if not _password_matches(account.password_hash, candidate_password):
        try:
            repository.record_customer_login_failure(account.id, now=now)
        except (PersistenceError, sqlite3.Error):
            return _login_response(
                error="Login is temporarily unavailable.",
                status_code=503,
                username=account.username,
                next_path=next_path,
            )
        return _login_response(
            error="Invalid username or password.",
            status_code=401,
            username=account.username,
            next_path=next_path,
        )

    try:
        repository.clear_customer_login_failures(account.id)
    except (PersistenceError, sqlite3.Error):
        return _login_response(
            error="Login is temporarily unavailable.",
            status_code=503,
            username=account.username,
            next_path=next_path,
        )
    _start_customer_session(account)
    return redirect(next_path or url_for("customer.account"), code=303)


@customer.get("/account")
def account():
    if g.customer is None:
        return redirect(
            url_for("customer.login", next=url_for("customer.account")), code=303
        )
    try:
        orders = _repository().list_customer_orders(g.customer.id)
    except (PersistenceError, sqlite3.Error):
        abort(503)
    return render_template("customer/account.html", private_page=True, orders=orders)


@customer.post("/logout")
def logout():
    try:
        require_csrf(request.form.get("csrf_token"))
    except FormSecurityError:
        abort(400)
    session.clear()
    return redirect(url_for("public.index"), code=303)


def register_customer_auth(app) -> None:
    app.register_blueprint(customer)


def _normalize_username(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if _USERNAME_PATTERN.fullmatch(normalized) else None


def _registration_error(
    username: str | None, password: str, confirmation: str
) -> str | None:
    if username is None:
        return (
            "Use 3–32 lowercase letters, numbers, periods, underscores, or hyphens. "
            "Start and end with a letter or number."
        )
    if not isinstance(password, str) or not isinstance(confirmation, str):
        return "The password is invalid."
    if len(password) < 12:
        return "Use at least 12 characters for the password."
    if len(password) > 1_024 or len(confirmation) > 1_024:
        return "The password is too long."
    if not secrets.compare_digest(password, confirmation):
        return "The passwords did not match."
    return None


def _password_matches(password_hash: str, candidate: str) -> bool:
    if not isinstance(candidate, str) or len(candidate) > 1_024:
        return False
    try:
        return check_password_hash(password_hash, candidate)
    except (ValueError, TypeError):
        return False


def _start_customer_session(account: CustomerAccount) -> None:
    session.clear()
    session[_CUSTOMER_SESSION_KEY] = {
        "id": account.id,
        "username": account.username,
        "credential_version": account.credential_version,
    }
    session.permanent = True


def _safe_next(value: str | None) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        return None
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _registration_response(
    *,
    error: str,
    status_code: int,
    username: str = "",
    next_path: str | None,
):
    return (
        render_template(
            "customer/register.html",
            error=error,
            username=username,
            next_path=next_path,
        ),
        status_code,
    )


def _login_response(
    *,
    error: str,
    status_code: int,
    username: str = "",
    next_path: str | None,
):
    return (
        render_template(
            "customer/login.html",
            error=error,
            username=username,
            next_path=next_path,
        ),
        status_code,
    )


def _repository() -> ServicesiteRepository:
    return current_app.extensions["servicesite_repository"]


def _now() -> datetime:
    factory = current_app.extensions.get(
        "servicesite_now_factory", lambda: datetime.now(timezone.utc)
    )
    return factory()
