from __future__ import annotations

import secrets

import pytest
from werkzeug.security import generate_password_hash

from app.checkout_details import CheckoutDetails, validate_delivery, validate_request
from app.payments.invoice import InvoiceCreator
from app.persistence import SCHEMA_VERSION
from test_cart_orders import CUSTOMER_ID, _add, _second_service, _tokens
from test_web_checkout import NOW, _service, _start_checkout, web_context


@pytest.mark.parametrize(("method", "address", "expected"), [
    ("email", " Person+project@EXAMPLE.COM ", "Person+project@example.com"),
    ("email", "first.last@sub.example.com", "first.last@sub.example.com"),
    ("telegram", "project_user", "@project_user"),
    ("telegram", " @ProjectUser ", "@ProjectUser"),
])
def test_delivery_contact_normalization(method, address, expected):
    assert validate_delivery(method, address) == expected


@pytest.mark.parametrize(("method", "address"), [
    ("sms", "123456789"), ("", "buyer@example.com"),
    ("email", ""), ("email", "not-an-email"), ("email", "a@@example.com"),
    ("email", ".name@example.com"), ("email", "a..b@example.com"),
    ("email", "a@-example.com"), ("email", "a@example..com"),
    ("email", "a@example"), ("email", "a@exa mple.com"),
    ("email", "x" * 65 + "@example.com"),
    ("email", "a@example.com\r\nBcc: other@example.com"),
    ("telegram", "abcd"), ("telegram", "1username"),
    ("telegram", "a" * 33), ("telegram", "name-with-dashes"),
    ("telegram", "https://t.me/username"), ("telegram", "@@username"),
    ("telegram", "user name"),
])
def test_invalid_delivery_formats_are_rejected(method, address):
    with pytest.raises(ValueError):
        validate_delivery(method, address)


def test_buy_now_merges_selected_service_into_existing_cart_without_invoice(web_context):
    _second_service(web_context)
    _add(web_context, "service-second", quantity=2)
    data = _start_checkout(web_context)
    cart = web_context.repository.get_cart(CUSTOMER_ID)
    assert {item.service_id: item.quantity for item in cart.items} == {
        "service-assessment": 1, "service-second": 2,
    }
    assert cart.total_usd_cents == 21_000
    assert "request_service-assessment" in data
    assert "request_service-second" in data
    assert web_context.repository.count_invoices() == 0
    assert web_context.wallet.calls == []
    assert web_context.rate_provider.calls == 0


def test_buy_now_keeps_existing_selected_quantity_and_revision(web_context):
    _add(web_context, quantity=3)
    before = web_context.repository.get_cart(CUSTOMER_ID)
    _start_checkout(web_context)
    assert web_context.repository.get_cart(CUSTOMER_ID) == before
    assert web_context.wallet.calls == []


def test_checkout_review_is_private_script_free_and_has_one_field_per_line(web_context):
    assert web_context.client.get("/cart/checkout").status_code == 303
    _add(web_context, quantity=3)
    response = web_context.client.get("/cart/checkout")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert body.count("<textarea") == 1
    assert "Cover all 3 units" in body
    assert 'name="delivery_method"' in body
    assert 'name="delivery_address"' in body
    assert "<script" not in body.lower()
    assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize(("field", "value"), [
    ("delivery_method", ""), ("delivery_method", "sms"),
    ("delivery_address", ""), ("delivery_address", "invalid"),
    ("request_service-assessment", ""), ("request_service-assessment", "   \n "),
    ("request_service-assessment", "x" * 4001),
    ("request_service-assessment", "review\x00request"),
], ids=["missing-method", "unknown-method", "missing-address", "invalid-address", "missing-request", "blank-request", "long-request", "control-character"])
def test_invalid_checkout_retains_cart_and_values_without_external_calls(web_context, field, value):
    data = _start_checkout(web_context)
    data[field] = value
    response = web_context.client.post("/cart/checkout", data=data)
    body = response.get_data(as_text=True)
    assert response.status_code == 400
    assert 'aria-invalid="true"' in body
    assert "Check the highlighted checkout fields" in body
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1
    assert web_context.repository.count_invoices() == 0
    assert web_context.wallet.calls == []
    assert web_context.rate_provider.calls == 0
    if field != "request_service-assessment":
        assert "Review the authorized test environment." in body
    if field != "delivery_address":
        assert 'value="buyer@example.com"' in body
    with web_context.client.session_transaction() as stored_session:
        assert "delivery_address" not in stored_session
        assert "item_requests" not in stored_session
    # The validation failure releases its claim and issues a fresh nonce.
    retry = _tokens(response, status_code=400)
    assert web_context.client.post("/cart/checkout", data=retry).status_code == 303


@pytest.mark.parametrize(("method", "address", "expected"), [
    ("email", "Person+project@EXAMPLE.COM", "Person+project@example.com"),
    ("telegram", "project_user", "@project_user"),
])
def test_details_saved_atomically_and_visible_only_to_owner_and_admin(web_context, method, address, expected):
    _second_service(web_context)
    _add(web_context, "service-second")
    data = _start_checkout(web_context)
    data.update({
        "delivery_method": method, "delivery_address": address,
        "request_service-assessment": "Authorized scope: staging only.\r\n<script>alert(1)</script>",
        "request_service-second": "Review access controls and provide a report.",
        "request_not-in-cart": "This must not be saved.",
    })
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    invoice_id = response.headers["Location"].split("/")[2]
    repo = web_context.repository
    details = repo.get_order_checkout_details(invoice_id)
    assert details.delivery_method == method
    assert details.delivery_address == expected
    assert details.requests_by_service == {
        "service-assessment": "Authorized scope: staging only.\n<script>alert(1)</script>",
        "service-second": "Review access controls and provide a report.",
    }
    assert expected not in repr(details)
    assert "staging only" not in repr(details)
    own = web_context.client.get(f"/account/orders/{invoice_id}")
    own_body = own.get_data(as_text=True)
    assert expected in own_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in own_body
    assert "<script>" not in own_body
    # A bearer payment page never includes private fulfillment instructions.
    payment_body = web_context.client.get(response.headers["Location"]).get_data(as_text=True)
    assert expected not in payment_body
    assert "staging only" not in payment_body
    other = web_context.app.test_client()
    token = _tokens(other.get("/register"))["csrf_token"]
    password = secrets.token_urlsafe(32)
    assert other.post("/register", data={
        "csrf_token": token, "username": "another.buyer",
        "password": password, "confirm_password": password,
    }).status_code == 303
    assert other.get(f"/account/orders/{invoice_id}").status_code == 404
    assert expected not in other.get(response.headers["Location"]).get_data(as_text=True)
    admin_password = secrets.token_urlsafe(32)
    repo.create_admin_credential(generate_password_hash(admin_password), now=NOW)
    admin = web_context.app.test_client()
    token = _tokens(admin.get("/admin/login"))["csrf_token"]
    assert admin.post("/admin/login", data={
        "csrf_token": token, "username": web_context.app.config["ADMIN_USERNAME"],
        "password": admin_password,
    }).status_code == 303
    page = admin.get(f"/admin/purchases/{invoice_id}")
    assert page.status_code == 200
    assert expected in page.get_data(as_text=True)
    assert "&lt;script&gt;" in page.get_data(as_text=True)


def test_delivery_storage_failure_rolls_back_invoice_items_and_cart_clear(web_context):
    data = _start_checkout(web_context)
    with web_context.repository.database.transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_request BEFORE INSERT ON order_item_requests
            BEGIN SELECT RAISE(ABORT, 'test rejection'); END""")
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 503
    repo = web_context.repository
    assert repo.count_invoices() == 0
    assert repo.get_cart(CUSTOMER_ID).quantity == 1
    with repo.database.transaction() as connection:
        for table in ("customer_orders", "invoice_items", "order_checkout_details", "order_item_requests"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_schema_seven_migration_preserves_existing_records_and_is_repeatable(web_context):
    _add(web_context, quantity=2)
    repo = web_context.repository
    creator = InvoiceCreator(repo, web_context.wallet, required_confirmations=10,
        sweep_required=False, now_factory=lambda: NOW)
    old_invoice = creator.create_invoice("service-assessment", web_context.rate_provider.get_quote(), customer_id=CUSTOMER_ID)
    before_cart = repo.get_cart(CUSTOMER_ID)
    before_items = repo.get_invoice_items(old_invoice.id)
    with repo.database.transaction() as connection:
        connection.execute("DROP TABLE order_item_requests")
        connection.execute("DROP TABLE order_checkout_details")
        connection.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
    repo.database.initialize()
    repo.database.initialize()
    assert repo.get_customer_order(CUSTOMER_ID, old_invoice.id) == old_invoice
    assert repo.get_cart(CUSTOMER_ID) == before_cart
    assert repo.get_invoice_items(old_invoice.id) == before_items
    assert repo.get_order_checkout_details(old_invoice.id) is None
    with repo.database.transaction() as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert web_context.client.get(f"/account/orders/{old_invoice.id}").status_code == 200


def test_details_must_match_order_services():
    details = CheckoutDetails("telegram", "@project_user", (("one", "Review authorized systems."),))
    with pytest.raises(ValueError):
        details.require_services(("two",))


def test_request_length_boundary_and_newline_normalization():
    assert validate_request("x" * 4000) == "x" * 4000
    assert validate_request(" First line.\r\nSecond line. ") == "First line.\nSecond line."


def test_full_cart_accepts_maximum_unicode_requests(web_context):
    _add(web_context)
    repo = web_context.repository
    for index in range(19):
        service_id = f"service-{index}"
        repo.insert_service(_service(service_id=service_id))
        repo.change_cart_item(CUSTOMER_ID, service_id, 1)
    review = web_context.client.get("/cart/checkout")
    assert 'enctype="multipart/form-data"' in review.get_data(as_text=True)
    data = _tokens(review)
    for name in data:
        if name.startswith("request_"):
            data[name] = "🔍" * 4000
    response = web_context.client.post("/cart/checkout", data=data, content_type="multipart/form-data")
    assert response.status_code == 303
    invoice_id = response.headers["Location"].split("/")[2]
    assert len(repo.get_order_checkout_details(invoice_id).item_requests) == 20


@pytest.mark.parametrize("streamed", [False, True])
def test_checkout_body_limit_rejects_oversized_request_before_external_calls(web_context, streamed):
    data = _start_checkout(web_context)
    data["request_service-assessment"] = "x" * (384 * 1024)
    response = web_context.client.post(
        "/cart/checkout", data=data, content_type="multipart/form-data",
        environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True} if streamed else {},
    )
    assert response.status_code == 413
    assert web_context.wallet.calls == []
    assert web_context.rate_provider.calls == 0
