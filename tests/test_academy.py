from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.academy import ACADEMY_CATEGORY_ID, ACADEMY_SERVICE_ID
from app.payments.invoice import XmrQuote
from app.payments.xmr_wallet_rpc import XmrSubaddress
from app.persistence import SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
CUSTOMER_PASSWORD = secrets.token_urlsafe(24)
ADMIN_PASSWORD = secrets.token_urlsafe(24)
TOKEN_PATTERN = re.compile(r'name="(?P<name>csrf_token|checkout_nonce)" value="(?P<value>[^"]+)"')
CAPTCHA_PATTERN = re.compile(r'class="captcha-question">(\d+) \+ (\d+)</strong>')


class FakeWallet:
    def create_subaddress(self, label):
        return XmrSubaddress(address="4" + ("8" * 94), account_index=7, address_index=1)


class FakeRateProvider:
    def get_quote(self):
        return XmrQuote(
            usd_per_xmr=Decimal("200"),
            source="academy-test-rate",
            quoted_at=NOW - timedelta(seconds=10),
        )


@pytest.fixture
def academy_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", secrets.token_hex(32))
    database = SQLiteDatabase(tmp_path / "academy.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.create_customer_account(
        customer_id="academy-customer-00000001",
        username="academy.student",
        password_hash=generate_password_hash(CUSTOMER_PASSWORD),
        now=NOW,
    )
    repository.create_admin_credential(generate_password_hash(ADMIN_PASSWORD), now=NOW)
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "academy.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_WALLET_CLIENT": FakeWallet(),
            "SERVICESITE_RATE_PROVIDER": FakeRateProvider(),
            "SERVICESITE_NOW_FACTORY": lambda: NOW,
            "ADMIN_USERNAME": "operator",
            "XMR_SWEEP_ENABLED": False,
        }
    )
    return app, app.test_client(), repository


def _tokens(response):
    return {
        match.group("name"): match.group("value")
        for match in TOKEN_PATTERN.finditer(response.get_data(as_text=True))
    }


def _login_customer(client):
    page = client.get("/login")
    captcha = CAPTCHA_PATTERN.search(page.get_data(as_text=True))
    assert captcha is not None
    token = _tokens(page)["csrf_token"]
    response = client.post(
        "/login",
        data={
            "csrf_token": token,
            "username": "academy.student",
            "password": CUSTOMER_PASSWORD,
            "captcha_answer": str(int(captcha.group(1)) + int(captcha.group(2))),
        },
    )
    assert response.status_code == 303


def _login_admin(client):
    page = client.get("/admin/login")
    response = client.post(
        "/admin/login",
        data={
            "csrf_token": _tokens(page)["csrf_token"],
            "username": "operator",
            "password": ADMIN_PASSWORD,
        },
    )
    assert response.status_code == 303


def test_academy_page_is_public_and_describes_authorized_program(academy_context):
    _app, client, repository = academy_context

    response = client.get("/academy")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sektor-7 Academy" in body
    assert "Train. Prove yourself. Stand out." in body
    assert "could be hired into Sektor-7" in body
    assert "Hiring is selective and is not guaranteed" in body
    assert "$300.00 USD total" in body
    assert "$100.00 USD now for month one" in body
    assert "No enrollment fee" in body
    assert "$500.00 USD" not in body
    assert "$1,500.00 USD" not in body
    assert "$5,000.00 USD" not in body
    assert "Log in to enroll" in body
    assert repository.get_purchasable_service(ACADEMY_SERVICE_ID) is None
    assert all(item.id != ACADEMY_CATEGORY_ID for item in repository.list_categories(include_archived=False) if item.published)
    anonymous_enroll = client.post("/academy/enroll", data={"tier": "operator"})
    assert anonymous_enroll.status_code == 303
    assert "/login?next=/academy" in anonymous_enroll.headers["Location"]


def test_customer_enrollment_creates_month_one_invoice_and_admin_record(academy_context):
    _app, client, repository = academy_context
    _login_customer(client)
    page = client.get("/academy")
    tokens = _tokens(page)

    response = client.post(
        "/academy/enroll",
        data={
            "csrf_token": tokens["csrf_token"],
            "checkout_nonce": tokens["checkout_nonce"],
            "tier": "black",
        },
    )
    enrollment = repository.get_academy_enrollment("academy-customer-00000001")

    assert response.status_code == 303
    assert "/checkout/" in response.headers["Location"]
    assert enrollment is not None
    assert enrollment.tier_key == "operator"
    assert enrollment.tuition_usd_cents == 30_000
    invoice = repository.get_customer_order(
        enrollment.customer_id, enrollment.enrollment_fee_invoice_id
    )
    assert invoice is not None
    assert invoice.price_usd_cents == 10_000
    assert invoice.service_id == ACADEMY_SERVICE_ID

    admin_client = _app.test_client()
    _login_admin(admin_client)
    admin_page = admin_client.get("/admin/academy")
    admin_body = admin_page.get_data(as_text=True)
    assert admin_page.status_code == 200
    assert "@academy.student" in admin_body
    assert "Academy Program" in admin_body
    assert "$300.00 USD" in admin_body
    assert "Month one" in admin_body
    assert "Awaiting payment" in admin_body


def test_enrollment_requires_valid_csrf_and_nonce(academy_context):
    _app, client, repository = academy_context
    _login_customer(client)

    assert client.post("/academy/enroll", data={"tier": "operator"}).status_code == 400
    assert repository.get_academy_enrollment("academy-customer-00000001") is None
    page = client.get("/academy")
    tokens = _tokens(page)
    valid = client.post(
        "/academy/enroll",
        data={
            "csrf_token": tokens["csrf_token"],
            "checkout_nonce": tokens["checkout_nonce"],
        },
    )

    assert valid.status_code == 303
    assert repository.get_academy_enrollment("academy-customer-00000001") is not None


def test_internal_academy_catalog_records_are_hidden_from_admin_catalog(academy_context):
    _app, client, _repository = academy_context
    _login_admin(client)

    assert "academy-enrollment-fee" not in client.get("/admin/services").get_data(as_text=True)
    assert client.get(f"/admin/services/{ACADEMY_SERVICE_ID}/edit").status_code == 404
    assert client.get(f"/admin/categories/{ACADEMY_CATEGORY_ID}/edit").status_code == 404


def test_cancelling_unpaid_fee_releases_academy_enrollment(academy_context):
    _app, client, repository = academy_context
    _login_customer(client)
    tokens = _tokens(client.get("/academy"))
    client.post(
        "/academy/enroll",
        data=tokens,
    )
    enrollment = repository.get_academy_enrollment("academy-customer-00000001")
    assert enrollment is not None

    repository.cancel_customer_order(
        enrollment.customer_id,
        enrollment.enrollment_fee_invoice_id,
        now=NOW + timedelta(minutes=1),
    )

    assert repository.get_academy_enrollment(enrollment.customer_id) is None
    assert client.get("/academy").status_code == 200


def test_schema_ten_upgrade_adds_academy_without_losing_accounts(tmp_path):
    database = SQLiteDatabase(tmp_path / "schema-ten.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.create_customer_account(
        customer_id="preserved-customer-00001",
        username="preserved.user",
        password_hash=generate_password_hash(CUSTOMER_PASSWORD),
        now=NOW,
    )
    with database.transaction() as connection:
        connection.execute("DROP TABLE academy_enrollments")
        connection.execute("DELETE FROM services WHERE id=?", (ACADEMY_SERVICE_ID,))
        connection.execute("DELETE FROM categories WHERE id=?", (ACADEMY_CATEGORY_ID,))
        connection.execute("UPDATE schema_meta SET value='10' WHERE key='schema_version'")

    database.initialize()

    assert repository.get_customer_account_by_username("preserved.user") is not None
    assert repository.get_academy_enrollment_service().price_usd_cents == 10_000
    with database.connect() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "11"
