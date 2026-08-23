from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from app.catalog import CategoryRecord, ServiceRecord
from app.payments.invoice import InvoiceCreator, PaymentStatus, XmrQuote
from app.payments.xmr_wallet_rpc import XmrSubaddress
from app.persistence import FulfillmentStatus, SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')


class FakeWallet:
    def create_subaddress(self, label):
        return XmrSubaddress(
            address="4" + ("7" * 94), account_index=7, address_index=1
        )


@pytest.fixture
def admin_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-admin-session-secret")
    database = SQLiteDatabase(tmp_path / "admin.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.create_admin_credential(
        generate_password_hash("correct horse battery staple"), now=NOW
    )
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "admin.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_NOW_FACTORY": lambda: NOW,
            "ADMIN_USERNAME": "operator",
            "ADMIN_SESSION_HOURS": 4,
        }
    )
    return app, app.test_client(), repository


@pytest.fixture
def uninitialized_admin_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-admin-session-secret")
    database = SQLiteDatabase(tmp_path / "uninitialized-admin.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "uninitialized-admin.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_NOW_FACTORY": lambda: NOW,
            "ADMIN_USERNAME": "operator",
            "ADMIN_SESSION_HOURS": 4,
        }
    )
    return app, app.test_client(), repository


def _csrf(response) -> str:
    match = CSRF_PATTERN.search(response.get_data(as_text=True))
    assert match is not None
    return match.group(1)


def _login(client):
    token = _csrf(client.get("/admin/login"))
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "username": "operator",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("/admin")
    return _csrf(client.get("/admin"))


def _category() -> CategoryRecord:
    return CategoryRecord(
        id="category-security",
        name="Security Services",
        slug="security-services",
        description="Authorized engagements.",
        published=True,
        archived=False,
        sort_order=10,
        created_at=NOW,
        updated_at=NOW,
    )


def _service() -> ServiceRecord:
    return ServiceRecord(
        id="service-assessment",
        category_id="category-security",
        name="Security Assessment",
        slug="security-assessment",
        description="Authorized assessment with report.",
        price_usd_cents=10_000,
        duration_label="One engagement",
        published=True,
        archived=False,
        sort_order=10,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _invoice(repository: ServicesiteRepository):
    repository.insert_category(_category())
    repository.insert_service(_service())
    creator = InvoiceCreator(
        repository,
        FakeWallet(),
        required_confirmations=10,
        sweep_required=False,
        invoice_ttl=timedelta(hours=2),
        maximum_quote_age=timedelta(minutes=5),
        now_factory=lambda: NOW,
        invoice_id_factory=lambda: "a" * 32,
        status_token_factory=lambda: "private-status-token-" + ("x" * 32),
    )
    return creator.create_invoice(
        "service-assessment",
        XmrQuote(
            usd_per_xmr=Decimal("200"),
            source="test-only-rate",
            quoted_at=NOW - timedelta(seconds=30),
        ),
    )


def test_admin_routes_require_login_and_private_headers(admin_context):
    _app, client, _repository = admin_context

    response = client.get("/admin")
    login = client.get("/admin/login")

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/admin/login")
    for item in (response, login):
        assert item.headers["Cache-Control"] == "no-store, private, max-age=0"
        assert item.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
        assert "script-src 'none'" in item.headers["Content-Security-Policy"]


def test_first_visit_sets_hashed_password_once_and_signs_in(uninitialized_admin_context):
    _app, client, repository = uninitialized_admin_context
    setup = client.get("/admin/login")
    body = setup.get_data(as_text=True)

    assert setup.status_code == 200
    assert "Create the administrator password" in body
    assert "operator" in body

    response = client.post(
        "/admin/login",
        data={
            "csrf_token": _csrf(setup),
            "new_password": "first password is long enough",
            "confirm_password": "first password is long enough",
        },
    )
    credential = repository.get_admin_credential()

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/admin")
    assert credential is not None
    assert "first password is long enough" not in credential.password_hash
    assert credential.password_hash.startswith("scrypt:")
    assert check_password_hash(
        credential.password_hash, "first password is long enough"
    )
    assert client.get("/admin").status_code == 200

    client.post("/admin/logout", data={"csrf_token": _csrf(client.get("/admin"))})
    login = client.get("/admin/login")
    assert "Administrator login" in login.get_data(as_text=True)
    assert "Create the administrator password" not in login.get_data(as_text=True)


def test_first_password_setup_validates_length_and_confirmation(
    uninitialized_admin_context,
):
    _app, client, repository = uninitialized_admin_context
    missing_csrf = client.post(
        "/admin/login",
        data={
            "new_password": "first password is long enough",
            "confirm_password": "first password is long enough",
        },
    )
    token = _csrf(client.get("/admin/login"))

    short = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "new_password": "too short",
            "confirm_password": "too short",
        },
    )
    mismatch = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "new_password": "long password number one",
            "confirm_password": "long password number two",
        },
    )

    assert missing_csrf.status_code == 400
    assert "Create the administrator password" in missing_csrf.get_data(as_text=True)
    assert short.status_code == 400
    assert mismatch.status_code == 400
    assert repository.get_admin_credential() is None


def test_login_requires_csrf_and_rate_limits_failures(admin_context):
    _app, client, _repository = admin_context

    assert client.post("/admin/login", data={}).status_code == 400
    token = _csrf(client.get("/admin/login"))
    for _ in range(5):
        response = client.post(
            "/admin/login",
            data={"csrf_token": token, "username": "operator", "password": "wrong"},
        )
        assert response.status_code == 401

    blocked = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "username": "operator",
            "password": "correct horse battery staple",
        },
    )
    assert blocked.status_code == 429
    assert "correct horse" not in blocked.get_data(as_text=True)


def test_login_logout_and_session_cookie(admin_context):
    _app, client, _repository = admin_context
    token = _login(client)

    dashboard = client.get("/admin")
    logout = client.post("/admin/logout", data={"csrf_token": token})

    assert dashboard.status_code == 200
    assert "Operations overview" in dashboard.get_data(as_text=True)
    assert logout.status_code == 303
    assert client.get("/admin").headers["Location"].endswith("/admin/login")


def test_password_change_requires_current_password_and_invalidates_sessions(
    admin_context,
):
    app, client, repository = admin_context
    other_client = app.test_client()
    _login(client)
    _login(other_client)
    password_page = client.get("/admin/password")

    wrong_current = client.post(
        "/admin/password",
        data={
            "csrf_token": _csrf(password_page),
            "current_password": "wrong current password",
            "new_password": "replacement password is long",
            "confirm_password": "replacement password is long",
        },
    )
    unchanged = repository.get_admin_credential()

    assert wrong_current.status_code == 400
    assert unchanged is not None and unchanged.version == 1
    assert check_password_hash(
        unchanged.password_hash, "correct horse battery staple"
    )

    changed = client.post(
        "/admin/password",
        data={
            "csrf_token": _csrf(client.get("/admin/password")),
            "current_password": "correct horse battery staple",
            "new_password": "replacement password is long",
            "confirm_password": "replacement password is long",
        },
    )
    credential = repository.get_admin_credential()

    assert changed.status_code == 303
    assert changed.headers["Location"].endswith("/admin/login")
    assert credential is not None and credential.version == 2
    assert check_password_hash(
        credential.password_hash, "replacement password is long"
    )
    assert client.get("/admin").headers["Location"].endswith("/admin/login")
    assert other_client.get("/admin").headers["Location"].endswith("/admin/login")

    token = _csrf(client.get("/admin/login"))
    old_login = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "username": "operator",
            "password": "correct horse battery staple",
        },
    )
    new_login = client.post(
        "/admin/login",
        data={
            "csrf_token": token,
            "username": "operator",
            "password": "replacement password is long",
        },
    )

    assert old_login.status_code == 401
    assert new_login.status_code == 303


def test_admin_creates_edits_and_archives_catalog_without_float_money(admin_context):
    _app, client, repository = admin_context
    token = _login(client)

    created_category = client.post(
        "/admin/categories/new",
        data={
            "csrf_token": token,
            "name": "Blue Team",
            "slug": "blue-team",
            "description": "Defensive security services.",
            "sort_order": "20",
            "published": "on",
        },
    )
    category = repository.list_categories()[0]
    created_service = client.post(
        "/admin/services/new",
        data={
            "csrf_token": token,
            "category_id": category.id,
            "name": "Configuration Review",
            "slug": "configuration-review",
            "description": "Review and prioritized findings.",
            "price_usd": "100.25",
            "duration_label": "One review",
            "sort_order": "10",
            "published": "on",
        },
    )
    service = repository.list_services()[0].service
    public_body = client.get("/").get_data(as_text=True)

    assert created_category.status_code == 303
    assert created_service.status_code == 303
    assert service.price_usd_cents == 10_025
    assert "Blue Team" in public_body
    assert "Configuration Review" in public_body
    assert "$100.25 USD" in public_body

    edited = client.post(
        f"/admin/services/{service.id}/edit",
        data={
            "csrf_token": token,
            "category_id": category.id,
            "name": "Configuration Review Plus",
            "slug": "configuration-review-plus",
            "description": "Expanded review.",
            "price_usd": "125.00",
            "duration_label": "One review",
            "sort_order": "10",
            "published": "on",
        },
    )
    updated = repository.get_service(service.id)
    archived = client.post(
        f"/admin/services/{service.id}/archive", data={"csrf_token": token}
    )

    assert edited.status_code == 303
    assert updated is not None and updated.version == 2
    assert updated.price_usd_cents == 12_500
    assert archived.status_code == 303
    assert "Configuration Review Plus" not in client.get("/").get_data(as_text=True)


def test_purchase_views_filter_redact_and_fulfill_only_settled(admin_context):
    _app, client, repository = admin_context
    invoice = _invoice(repository)
    token = _login(client)

    listing = client.get("/admin/purchases?payment_status=awaiting_payment")
    body = listing.get_data(as_text=True)
    blocked = client.post(
        f"/admin/purchases/{invoice.id}/fulfill",
        data={"csrf_token": token, "note": "must not apply"},
    )

    assert listing.status_code == 200
    assert invoice.id[:12] in body
    assert invoice.status_token not in body
    assert invoice.xmr_address not in body
    assert blocked.status_code == 303
    assert repository.get_admin_purchase(invoice.id).fulfillment_status is FulfillmentStatus.UNFULFILLED

    repository.record_observation(
        invoice.id,
        observed_atomic=invoice.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-deposit-id",
        now=NOW + timedelta(minutes=1),
    )
    repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    repository.transition_status(
        invoice.id, PaymentStatus.SETTLED, now=NOW + timedelta(minutes=2)
    )
    fulfilled = client.post(
        f"/admin/purchases/{invoice.id}/fulfill",
        data={"csrf_token": token, "note": "Report delivered securely."},
    )
    purchase = repository.get_admin_purchase(invoice.id)
    status_body = client.get(
        f"/status/{invoice.id}/{invoice.status_token}"
    ).get_data(as_text=True)

    assert fulfilled.status_code == 303
    assert purchase.fulfillment_status is FulfillmentStatus.FULFILLED
    assert purchase.fulfillment_note == "Report delivered securely."
    assert "Service fulfillment has been marked complete." in status_body
    assert "test-only-deposit-id" not in client.get(
        f"/admin/purchases/{invoice.id}"
    ).get_data(as_text=True)
