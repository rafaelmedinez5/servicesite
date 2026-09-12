from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.inquiries import InquiryStore
from app.persistence import SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 9, 12, 20, 0, tzinfo=timezone.utc)
CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
PASSWORD = "correct horse battery staple"
ADMIN_PASSWORD = "administrator password for inquiry tests"


@pytest.fixture
def inquiry_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-inquiry-session-secret")
    database = SQLiteDatabase(tmp_path / "inquiries.db")
    database.initialize()
    InquiryStore(database).initialize()
    repository = ServicesiteRepository(database)
    account = repository.create_customer_account(
        customer_id="customer-inquiry-tests-000001",
        username="inquiry.user",
        password_hash=generate_password_hash(PASSWORD),
        now=NOW,
    )
    assert account is not None
    repository.create_admin_credential(generate_password_hash(ADMIN_PASSWORD), now=NOW)
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(database.path),
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


def _login_customer(client):
    token = _csrf(client.get("/login"))
    response = client.post(
        "/login",
        data={"csrf_token": token, "username": "inquiry.user", "password": PASSWORD},
    )
    assert response.status_code == 303


def _login_admin(client):
    token = _csrf(client.get("/admin/login"))
    response = client.post(
        "/admin/login",
        data={"csrf_token": token, "username": "operator", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 303


def test_public_pages_remain_readable_but_submission_requires_account(inquiry_context):
    _app, client, _repository = inquiry_context
    assert client.get("/contact").status_code == 200
    assert client.get("/join").status_code == 200

    contact = client.post("/contact", data={})
    join = client.post("/join", data={})
    assert contact.status_code == 303
    assert "/login?next=/contact" in contact.headers["Location"]
    assert join.status_code == 303
    assert "/login?next=/join" in join.headers["Location"]


def test_contact_submission_uses_csrf_validation_and_treats_sql_html_as_data(inquiry_context):
    _app, client, _repository = inquiry_context
    _login_customer(client)
    page = client.get("/contact")
    malicious = "Need help with x'); DROP TABLE customer_accounts; -- <script>alert(1)</script>"

    missing_csrf = client.post(
        "/contact",
        data={
            "topic": "technical",
            "reply_method": "email",
            "reply_address": "person@example.com",
            "message": malicious,
        },
    )
    assert missing_csrf.status_code == 400

    response = client.post(
        "/contact",
        data={
            "csrf_token": _csrf(page),
            "topic": "technical",
            "reply_method": "email",
            "reply_address": "person@example.com",
            "message": malicious,
        },
    )
    assert response.status_code == 303

    database = _repository.database.connect()
    try:
        assert database.execute("SELECT COUNT(*) FROM customer_accounts").fetchone()[0] == 1
        assert database.execute("SELECT message FROM contact_inquiries").fetchone()[0] == malicious
    finally:
        database.close()


def test_join_application_is_database_enforced_once_per_account(inquiry_context):
    _app, client, repository = inquiry_context
    _login_customer(client)
    data = {
        "specialty": "infrastructure",
        "reply_method": "telegram",
        "reply_address": "@example_user",
        "skills": "I design hardened infrastructure and document operational controls.",
        "tools": "Linux, Tor, systemd, Python",
        "motivation": "I want to contribute to authorized projects with clear scope and review.",
    }

    first_page = client.get("/join")
    first = client.post("/join", data={"csrf_token": _csrf(first_page), **data})
    second_page = client.get("/join")
    second = client.post("/join", data={"csrf_token": _csrf(second_page), **data})

    assert first.status_code == 303
    assert second.status_code == 303
    assert "Application already submitted" in client.get("/join").get_data(as_text=True)
    connection = repository.database.connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM join_applications").fetchone()[0] == 1
    finally:
        connection.close()


def test_contact_rate_limit_allows_five_per_hour(inquiry_context):
    _app, client, _repository = inquiry_context
    _login_customer(client)
    for number in range(5):
        page = client.get("/contact")
        response = client.post(
            "/contact",
            data={
                "csrf_token": _csrf(page),
                "topic": "other",
                "reply_method": "email",
                "reply_address": "person@example.com",
                "message": f"Contact request number {number} for rate limit coverage.",
            },
        )
        assert response.status_code == 303

    page = client.get("/contact")
    blocked = client.post(
        "/contact",
        data={
            "csrf_token": _csrf(page),
            "topic": "other",
            "reply_method": "email",
            "reply_address": "person@example.com",
            "message": "Sixth request should be rate limited for this account.",
        },
    )
    assert blocked.status_code == 429


def test_admin_inquiry_view_requires_admin_and_autoescapes_user_text(inquiry_context):
    app, customer_client, repository = inquiry_context
    _login_customer(customer_client)
    page = customer_client.get("/contact")
    customer_client.post(
        "/contact",
        data={
            "csrf_token": _csrf(page),
            "topic": "service",
            "reply_method": "email",
            "reply_address": "person@example.com",
            "message": "Question <script>alert('xss')</script> about a service.",
        },
    )

    admin_client = app.test_client()
    anonymous = admin_client.get("/admin/inquiries")
    assert anonymous.status_code == 303
    assert anonymous.headers["Location"].endswith("/admin/login")

    _login_admin(admin_client)
    response = admin_client.get("/admin/inquiries")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "@inquiry.user" in body
    assert "&lt;script&gt;alert" in body
    assert "<script>alert('xss')</script>" not in body
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
