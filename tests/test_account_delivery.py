from __future__ import annotations

import re
import secrets
import sqlite3

import pytest
from werkzeug.security import generate_password_hash

from app.checkout_details import validate_delivery, validate_request
from app.deliveries import DeliveryValidationError, MAX_DELIVERY_BODY_BYTES
from app.payments.invoice import PaymentStatus
from app.persistence import FulfillmentStatus, SCHEMA_VERSION
from test_cart_orders import CUSTOMER_ID, _tokens
from test_web_checkout import NOW, _start_checkout, web_context


def _account_order(context):
    data = _start_checkout(context)
    data.update(delivery_method="account", delivery_address="unused@example.com")
    data["request_service-assessment"] = ""
    response = context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    return context.repository.get_invoice(response.headers["Location"].split("/")[2]), response


def _settle(context, invoice):
    repo = context.repository
    repo.record_observation(invoice.id, observed_atomic=invoice.expected_atomic,
        observed_confirmations=10, deposit_txid="test-only", now=NOW)
    repo.transition_status(invoice.id, PaymentStatus.PAID_PENDING_CONFIRMATIONS, now=NOW)
    repo.transition_status(invoice.id, PaymentStatus.SETTLED, now=NOW)


def _admin(context):
    password = secrets.token_urlsafe(32)
    context.repository.create_admin_credential(generate_password_hash(password), now=NOW)
    client = context.app.test_client()
    csrf = _tokens(client.get("/admin/login"))["csrf_token"]
    assert client.post("/admin/login", data={
        "csrf_token": csrf, "username": context.app.config["ADMIN_USERNAME"], "password": password,
    }).status_code == 303
    return client, _tokens(client.get("/admin"))["csrf_token"]


def test_checkout_pgp_hint_optional_requests_and_account_contact(web_context):
    _start_checkout(web_context)
    body = web_context.client.get("/cart/checkout").get_data(as_text=True)
    assert "public PGP key" in body
    assert 'href="/pgp-key" target="_blank" rel="noopener noreferrer"' in body
    assert "Leave blank if no instructions are needed" in body
    assert "Do not include passwords or private keys" not in body
    assert not any("required" in tag for tag in re.findall(r"<textarea[^>]*>", body))
    assert 'value="account"' in body
    assert validate_request("  \n ") == ""
    assert validate_delivery("account", "unused@example.com") == ""
    invoice, _ = _account_order(web_context)
    details = web_context.repository.get_order_checkout_details(invoice.id)
    assert details.delivery_address == ""
    assert details.requests_by_service["service-assessment"] == ""
    order = web_context.client.get(f"/account/orders/{invoice.id}").get_data(as_text=True)
    assert "My account" in order and "No delivery yet" in order
    assert "No additional instructions" in order
    assert "unused@example.com" not in order
    assert "View order and delivery" in web_context.client.get("/account").get_data(as_text=True)


def test_account_delivery_requires_admin_csrf_and_settled_payment(web_context):
    invoice, _ = _account_order(web_context)
    url = f"/admin/purchases/{invoice.id}/fulfill"
    assert web_context.app.test_client().post(url, data={"delivery_body": "Report."}).status_code == 303
    assert web_context.client.post(url, data={"delivery_body": "Report."}).status_code == 303
    admin, csrf = _admin(web_context)
    assert admin.post(url, data={"delivery_body": "Report."}).status_code == 400
    assert admin.post(url, data={"csrf_token": csrf, "delivery_body": "Report."}).status_code == 303
    assert web_context.repository.get_account_delivery(invoice.id) is None
    assert web_context.repository.get_admin_purchase(invoice.id).fulfillment_status is FulfillmentStatus.UNFULFILLED


def test_publish_delivery_owner_only_escaped_and_not_overwritten(web_context):
    invoice, response = _account_order(web_context)
    _settle(web_context, invoice)
    admin, csrf = _admin(web_context)
    url = f"/admin/purchases/{invoice.id}/fulfill"
    delivery = "Completed report.\n<script>untrusted()</script>"
    assert admin.post(url, data={
        "csrf_token": csrf, "delivery_body": delivery, "note": "Private operator note.",
    }).status_code == 303
    stored = web_context.repository.get_account_delivery(invoice.id)
    assert stored.body == delivery
    assert "Completed report" not in repr(stored)
    own = web_context.client.get(f"/account/orders/{invoice.id}")
    body = own.get_data(as_text=True)
    assert "Completed report" in body and "&lt;script&gt;" in body
    assert "<script>" not in body and "Private operator note" not in body
    assert own.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert "Completed report" in admin.get(f"/admin/purchases/{invoice.id}").get_data(as_text=True)
    assert "Completed report" not in web_context.client.get(response.headers["Location"]).get_data(as_text=True)
    assert "Completed report" not in web_context.client.get(
        f"/status/{invoice.id}/{invoice.status_token}"
    ).get_data(as_text=True)
    other = web_context.app.test_client()
    csrf_other = _tokens(other.get("/register"))["csrf_token"]
    password = secrets.token_urlsafe(32)
    other.post("/register", data={"csrf_token": csrf_other, "username": "another.buyer",
        "password": password, "confirm_password": password})
    assert other.get(f"/account/orders/{invoice.id}").status_code == 404
    assert "Completed report" not in other.get(response.headers["Location"]).get_data(as_text=True)
    assert admin.post(url, data={"csrf_token": csrf, "delivery_body": "Replacement."}).status_code == 303
    assert web_context.repository.get_account_delivery(invoice.id) == stored


@pytest.mark.parametrize("message", ["", "   ", "x" * 12001, "text\x00text"], ids=["missing", "blank", "too-long", "control"])
def test_invalid_delivery_keeps_order_unfulfilled_and_preserves_note(web_context, message):
    invoice, _ = _account_order(web_context)
    _settle(web_context, invoice)
    admin, csrf = _admin(web_context)
    response = admin.post(f"/admin/purchases/{invoice.id}/fulfill", data={
        "csrf_token": csrf, "delivery_body": message, "note": "Keep this note.",
    }, content_type="multipart/form-data")
    assert response.status_code == 400
    assert "Keep this note." in response.get_data(as_text=True)
    assert web_context.repository.get_account_delivery(invoice.id) is None
    assert web_context.repository.get_admin_purchase(invoice.id).fulfillment_status is FulfillmentStatus.UNFULFILLED


def test_account_delivery_and_fulfillment_are_atomic(web_context):
    invoice, _ = _account_order(web_context)
    _settle(web_context, invoice)
    admin, csrf = _admin(web_context)
    with web_context.repository.database.transaction() as connection:
        connection.execute("""CREATE TRIGGER fail_fulfill BEFORE UPDATE OF fulfillment_status ON invoices
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    response = admin.post(f"/admin/purchases/{invoice.id}/fulfill", data={
        "csrf_token": csrf, "delivery_body": "Keep this delivery.",
    })
    assert response.status_code == 503
    assert "Keep this delivery." in response.get_data(as_text=True)
    assert web_context.repository.get_admin_purchase(invoice.id).fulfillment_status is FulfillmentStatus.UNFULFILLED
    with web_context.repository.database.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM account_deliveries").fetchone()[0] == 0


def test_maximum_unicode_delivery_and_note_fit_multipart_form(web_context):
    invoice, _ = _account_order(web_context)
    _settle(web_context, invoice)
    admin, csrf = _admin(web_context)
    delivery = "\U0001f4e6" * 12000
    response = admin.post(f"/admin/purchases/{invoice.id}/fulfill", data={
        "csrf_token": csrf, "delivery_body": delivery, "note": "\U0001f4e6" * 2000,
    }, content_type="multipart/form-data")
    assert response.status_code == 303
    assert web_context.repository.get_account_delivery(invoice.id).body == delivery


def test_oversized_delivery_body_is_rejected_before_fulfillment(web_context):
    invoice, _ = _account_order(web_context)
    _settle(web_context, invoice)
    admin, csrf = _admin(web_context)
    response = admin.post(f"/admin/purchases/{invoice.id}/fulfill", data={
        "csrf_token": csrf, "delivery_body": "x" * MAX_DELIVERY_BODY_BYTES,
    }, content_type="multipart/form-data")
    assert response.status_code == 413
    assert web_context.repository.get_account_delivery(invoice.id) is None
    assert web_context.repository.get_admin_purchase(invoice.id).fulfillment_status is FulfillmentStatus.UNFULFILLED


def test_absent_item_request_is_saved_as_blank(web_context):
    data = _start_checkout(web_context)
    data.pop("request_service-assessment")
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    invoice_id = response.headers["Location"].split("/")[2]
    assert web_context.repository.get_order_checkout_details(invoice_id).requests_by_service == {
        "service-assessment": "",
    }


def test_non_account_orders_cannot_publish_account_delivery(web_context):
    data = _start_checkout(web_context)
    response = web_context.client.post("/cart/checkout", data=data)
    invoice = web_context.repository.get_invoice(response.headers["Location"].split("/")[2])
    _settle(web_context, invoice)
    with pytest.raises(DeliveryValidationError):
        web_context.repository.mark_purchase_fulfilled(invoice.id, note="", delivery_body="Report.", now=NOW)


V8_CHECKOUT_TABLES = """
CREATE TABLE order_checkout_details (
    invoice_id TEXT PRIMARY KEY REFERENCES customer_orders(invoice_id) ON DELETE RESTRICT,
    delivery_method TEXT NOT NULL CHECK (delivery_method IN ('email', 'telegram')),
    delivery_address TEXT NOT NULL CHECK (length(delivery_address) BETWEEN 1 AND 254)
);
CREATE TABLE order_item_requests (
    invoice_id TEXT NOT NULL REFERENCES order_checkout_details(invoice_id) ON DELETE RESTRICT,
    service_id TEXT NOT NULL,
    request_text TEXT NOT NULL CHECK (length(request_text) BETWEEN 1 AND 4000),
    PRIMARY KEY (invoice_id, service_id),
    FOREIGN KEY (invoice_id, service_id) REFERENCES invoice_items(invoice_id, service_id) ON DELETE RESTRICT
);
"""


def _v8_order(context):
    db = context.repository.database
    with db.transaction() as connection:
        connection.execute("DROP TABLE account_deliveries")
        connection.execute("DROP TABLE order_item_requests")
        connection.execute("DROP TABLE order_checkout_details")
        connection.executescript(V8_CHECKOUT_TABLES)
        connection.execute("UPDATE schema_meta SET value='8' WHERE key='schema_version'")
    data = _start_checkout(context)
    response = context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    return context.repository.get_invoice(response.headers["Location"].split("/")[2])


def test_schema_eight_upgrade_preserves_orders_requests_and_carts(web_context):
    invoice = _v8_order(web_context)
    repo = web_context.repository
    before_details = repo.get_order_checkout_details(invoice.id)
    before_items = repo.get_invoice_items(invoice.id)
    before_account = repo.get_customer_account_by_id(CUSTOMER_ID)
    repo.change_cart_item(CUSTOMER_ID, "service-assessment", 3)
    before_cart = repo.get_cart(CUSTOMER_ID)
    repo.database.initialize()
    repo.database.initialize()
    assert repo.get_invoice(invoice.id) == invoice
    assert repo.get_order_checkout_details(invoice.id) == before_details
    assert repo.get_invoice_items(invoice.id) == before_items
    assert repo.get_customer_account_by_id(CUSTOMER_ID) == before_account
    assert repo.get_cart(CUSTOMER_ID) == before_cart
    with repo.database.transaction() as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    _account_order(web_context)


def test_failed_constraint_migration_rolls_back_and_can_retry(web_context, monkeypatch):
    import app.persistence as persistence
    invoice = _v8_order(web_context)
    original = persistence._upgrade_checkout_constraints
    def fail_after_rebuild(connection):
        original(connection)
        raise RuntimeError("test-only migration interruption")
    monkeypatch.setattr(persistence, "_upgrade_checkout_constraints", fail_after_rebuild)
    with pytest.raises(RuntimeError):
        web_context.repository.database.initialize()
    with web_context.repository.database.transaction() as connection:
        assert connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "8"
        assert connection.execute("SELECT count(*) FROM order_item_requests").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE order_item_requests SET request_text='' WHERE invoice_id=?", (invoice.id,))
    monkeypatch.setattr(persistence, "_upgrade_checkout_constraints", original)
    web_context.repository.database.initialize()
    assert web_context.repository.get_invoice(invoice.id) == invoice
