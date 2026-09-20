from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from app.persistence import SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)
CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
CAPTCHA_PATTERN = re.compile(r'class="captcha-question">(\d+) \+ (\d+)</strong>')


@pytest.fixture
def customer_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-customer-session-secret")
    database = SQLiteDatabase(tmp_path / "customers.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "customers.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_NOW_FACTORY": lambda: NOW,
            "ADMIN_SESSION_HOURS": 4,
        }
    )
    return app, app.test_client(), repository


def _csrf(response) -> str:
    match = CSRF_PATTERN.search(response.get_data(as_text=True))
    assert match is not None
    return match.group(1)


def _captcha(response) -> str:
    match = CAPTCHA_PATTERN.search(response.get_data(as_text=True))
    assert match is not None
    return str(int(match.group(1)) + int(match.group(2)))


def _create_customer(repository, *, username="registered.user"):
    account = repository.create_customer_account(
        customer_id=f"customer-{username}-00000001",
        username=username,
        password_hash=generate_password_hash("correct horse battery staple"),
        now=NOW,
    )
    assert account is not None
    return account


def _login(
    client,
    *,
    username="registered.user",
    password="correct horse battery staple",
    next_path=None,
):
    page = client.get("/login")
    data = {
        "csrf_token": _csrf(page),
        "username": username,
        "password": password,
        "captcha_answer": _captcha(page),
    }
    if next_path is not None:
        data["next"] = next_path
    return client.post("/login", data=data)


def test_auth_pages_are_private_and_navigation_offers_account_creation(
    customer_context,
):
    _, client, _ = customer_context

    home = client.get("/")
    register = client.get("/register")
    login = client.get("/login")

    assert home.status_code == 200
    assert "Sektor-7 provides targeted, surgical security engagements" in home.get_data(as_text=True)
    login_body = login.get_data(as_text=True)
    register_body = register.get_data(as_text=True)
    assert 'href="/login"' in login_body
    assert 'href="/register"' in login_body
    assert 'href="/about"' in login_body
    assert 'class="nav-dropdown"' not in login_body
    assert 'pattern="[a-z0-9][a-z0-9._\\-]{1,30}[a-z0-9]"' in register_body
    assert "browse services, manage your cart, and follow your orders" in login_body
    assert "create a Monero invoice" not in login_body
    assert "one-way hashes" not in register_body
    assert "never cached" not in register_body
    assert 'name="captcha_answer"' in login_body
    assert CAPTCHA_PATTERN.search(login_body)
    for body in (register_body, login_body):
        assert 'class="auth-brand-icon"' in body
        assert 'src="/static/branding/sektor7-icon.png"' in body
    for response in (register, login):
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
        assert (
            '<meta name="robots" content="noindex, nofollow, noarchive">'
            in response.get_data(as_text=True)
        )


def test_site_entry_captcha_is_required_correct_and_single_use(customer_context):
    app, client, _ = customer_context
    app.config["SITE_ACCESS_GATE_ENABLED"] = True
    page = client.get("/")
    data = {"csrf_token": _csrf(page)}

    missing = client.post("/", data=data)
    assert missing.status_code == 400
    assert "verification answer" in missing.get_data(as_text=True)

    fresh = missing
    successful = client.post(
        "/",
        data={
            "csrf_token": _csrf(fresh),
            "captcha_answer": _captcha(fresh),
        },
    )
    assert successful.status_code == 303
    assert successful.headers["Location"].endswith("/")

    homepage = client.get("/")
    assert homepage.status_code == 200
    body = homepage.get_data(as_text=True)
    assert "Sektor-7 provides targeted, surgical security engagements" in body
    assert "Confirm you are human" not in body

    with client.session_transaction() as browser_session:
        browser_session.pop("_site_access_verified")
    replay = client.post(
        "/",
        data={
            "csrf_token": _csrf(fresh),
            "captcha_answer": _captcha(fresh),
        },
    )
    assert replay.status_code == 400


def test_site_entry_gate_blocks_every_browser_page_until_verified(customer_context):
    app, client, _ = customer_context
    app.config["SITE_ACCESS_GATE_ENABLED"] = True

    for path in (
        "/login",
        "/register",
        "/about",
        "/academy",
        "/join",
        "/contact",
        "/pgp-key",
        "/services/example",
        "/admin/login",
        "/missing-page",
    ):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["Location"].endswith("/")

    assert client.get("/static/css/style.css").status_code == 200
    assert client.get("/health").status_code == 200

    entry = client.get("/")
    verified = client.post(
        "/",
        data={
            "csrf_token": _csrf(entry),
            "captcha_answer": _captcha(entry),
        },
    )
    assert verified.status_code == 303
    assert client.get("/about").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/admin/login").status_code == 200


def test_registration_normalizes_username_hashes_password_and_starts_session(
    customer_context,
):
    _, client, repository = customer_context
    token = _csrf(client.get("/register"))

    response = client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "  New.Customer  ",
            "password": "a unique customer password",
            "confirm_password": "a unique customer password",
        },
    )

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/account")
    account = repository.get_customer_account_by_username("new.customer")
    assert account is not None
    assert account.password_hash != "a unique customer password"
    assert check_password_hash(account.password_hash, "a unique customer password")
    account_page = client.get("/account")
    assert account_page.status_code == 200
    account_body = account_page.get_data(as_text=True)
    assert "Signed in as @new.customer" in account_body
    assert 'class="account-dropdown"' in account_body
    assert 'href="/account">Account overview</a>' in account_body
    assert 'href="/account#orders">Orders</a>' in account_body
    assert 'action="/logout"' in account_body
    assert "Account security" not in account_body
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


@pytest.mark.parametrize(
    ("username", "password", "confirmation", "message"),
    [
        (
            "ab",
            "a unique customer password",
            "a unique customer password",
            "Use 3–32 lowercase",
        ),
        ("valid.user", "too-short", "too-short", "at least 12 characters"),
        (
            "valid.user",
            "a unique customer password",
            "a different customer password",
            "did not match",
        ),
    ],
)
def test_registration_rejects_invalid_credentials(
    customer_context, username, password, confirmation, message
):
    _, client, repository = customer_context
    token = _csrf(client.get("/register"))

    response = client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "confirm_password": confirmation,
        },
    )

    assert response.status_code == 400
    assert message in response.get_data(as_text=True)
    assert repository.get_customer_account_by_username("valid.user") is None


def test_registration_enforces_case_insensitive_unique_usernames(customer_context):
    _, client, repository = customer_context
    _create_customer(repository, username="existing.user")
    token = _csrf(client.get("/register"))

    response = client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": "Existing.User",
            "password": "another customer password",
            "confirm_password": "another customer password",
        },
    )

    assert response.status_code == 409
    assert "username is unavailable" in response.get_data(as_text=True)


def test_login_uses_generic_errors_supports_local_next_and_logout(customer_context):
    _, client, repository = customer_context
    _create_customer(repository)

    missing = _login(client, username="missing.user", password="wrong password value")
    wrong = _login(client, password="wrong password value")
    successful = _login(
        client,
        username="REGISTERED.USER",
        next_path="/services/example-service?from=login#discarded",
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert "Invalid username or password." in missing.get_data(as_text=True)
    assert "Invalid username or password." in wrong.get_data(as_text=True)
    assert successful.status_code == 303
    assert successful.headers["Location"].endswith(
        "/services/example-service?from=login"
    )
    account = client.get("/account")
    assert account.status_code == 200
    assert "@registered.user" in account.get_data(as_text=True)

    logout = client.post("/logout", data={"csrf_token": _csrf(account)})
    assert logout.status_code == 303
    assert logout.headers["Location"].endswith("/login")
    assert client.get("/account").headers["Location"].endswith(
        "/login?next=/account"
    )


def test_login_captcha_is_required_correct_and_single_use(customer_context):
    _, client, repository = customer_context
    _create_customer(repository)
    page = client.get("/login")
    data = {
        "csrf_token": _csrf(page),
        "username": "registered.user",
        "password": "correct horse battery staple",
    }

    missing = client.post("/login", data=data)
    assert missing.status_code == 400
    assert "verification answer" in missing.get_data(as_text=True)

    replay = client.post(
        "/login", data={**data, "captcha_answer": _captcha(page)}
    )
    assert replay.status_code == 400

    fresh = client.get("/login")
    successful = client.post(
        "/login",
        data={
            **data,
            "csrf_token": _csrf(fresh),
            "captcha_answer": _captcha(fresh),
        },
    )
    assert successful.status_code == 303


def test_login_rejects_external_next_destination(customer_context):
    _, client, repository = customer_context
    _create_customer(repository)

    response = _login(client, next_path="https://example.invalid/steal")

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/account")


def test_customer_login_is_blocked_after_five_failures(customer_context):
    _, client, repository = customer_context
    _create_customer(repository)

    for _ in range(5):
        response = _login(client, password="definitely the wrong password")
        assert response.status_code == 401
    blocked = _login(client)

    assert blocked.status_code == 429
    assert "Wait 15 minutes" in blocked.get_data(as_text=True)


def test_customer_forms_require_csrf(customer_context):
    _, client, repository = customer_context
    _create_customer(repository)

    register = client.post(
        "/register",
        data={
            "username": "new.customer",
            "password": "a unique customer password",
            "confirm_password": "a unique customer password",
        },
    )
    login = client.post(
        "/login",
        data={
            "username": "registered.user",
            "password": "correct horse battery staple",
        },
    )
    _login(client)
    logout = client.post("/logout", data={})

    assert register.status_code == 400
    assert login.status_code == 400
    assert logout.status_code == 400


def test_anonymous_access_is_limited_to_entry_and_information_pages(customer_context):
    _, client, _ = customer_context

    entry = client.get("/")
    assert entry.status_code == 200
    assert "Sektor-7 provides targeted, surgical security engagements" in entry.get_data(as_text=True)

    for path in (
        "/categories/example",
        "/services/example",
        "/cart",
        "/account",
    ):
        response = client.get(path)
        assert response.status_code == 303
        assert response.headers["Location"].startswith("/login?next=")

    for path in (
        "/login",
        "/register",
        "/about",
        "/join",
        "/contact",
        "/pgp-key",
        "/admin/login",
        "/health",
    ):
        assert client.get(path).status_code == 200
