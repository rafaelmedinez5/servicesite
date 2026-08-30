from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

import pytest

from app.orders import CartChangedError, CheckoutInProgressError
from app.payments.invoice import InvoiceCreator, PaymentStatus
from app.payments.xmr_rate import XmrRateUnavailableError
from app.payments.xmr_wallet_rpc import XmrWalletRpcError
from app.persistence import FulfillmentNotAllowedError, PersistenceError
from test_web_checkout import NOW, _category, _login_customer, _service, web_context


FORM_TOKENS = re.compile(
    r'name="(csrf_token|checkout_nonce|cart_version|cart_fingerprint)" value="([^"]+)"'
)
CUSTOMER_ID = "customer-test-00000001"


def _tokens(response):
    assert response.status_code == 200
    return dict(FORM_TOKENS.findall(response.get_data(as_text=True)))


def _add(context, service_id="service-assessment", quantity=1):
    _login_customer(context.client)
    token = _tokens(context.client.get("/cart"))["csrf_token"]
    response = context.client.post("/cart/add", data={
        "csrf_token": token, "service_id": service_id, "quantity": str(quantity),
    })
    assert response.status_code == 303


def _second_service(context):
    context.repository.insert_category(replace(
        _category(), id="category-second", slug="second", name="Second category"
    ))
    context.repository.insert_service(replace(
        _service(service_id="service-second"), category_id="category-second",
        name="Second assessment", price_usd_cents=5_500,
    ))


def _checkout(context):
    data = _tokens(context.client.get("/cart"))
    response = context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    invoice = context.repository.get_invoice(response.headers["Location"].split("/")[2])
    return invoice, response, data


def test_cart_is_saved_across_customer_sessions_and_can_update_or_remove(web_context):
    _add(web_context, quantity=2)
    client = web_context.client
    token = _tokens(client.get("/cart"))["csrf_token"]
    assert client.post("/cart/items/service-assessment", data={
        "csrf_token": token, "quantity": "3",
    }).status_code == 303
    client.post("/logout", data={"csrf_token": token})
    _login_customer(client)
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 3
    token = _tokens(client.get("/cart"))["csrf_token"]
    assert client.post("/cart/items/service-assessment", data={
        "csrf_token": token, "quantity": "0",
    }).status_code == 303
    assert "Your cart is empty" in client.get("/cart").get_data(as_text=True)


@pytest.mark.parametrize("quantity", ["-1", "1.5", "11", "abc", "999999999999999999"])
def test_invalid_quantities_do_not_change_cart(web_context, quantity):
    _add(web_context)
    token = _tokens(web_context.client.get("/cart"))["csrf_token"]
    response = web_context.client.post("/cart/items/service-assessment", data={
        "csrf_token": token, "quantity": quantity,
    })
    assert response.status_code == 400
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1


def test_anonymous_shopping_and_missing_csrf_cannot_mutate_cart(web_context):
    client = web_context.client
    for url in ("/cart", "/account/orders/not-an-order"):
        assert client.get(url).status_code == 303
    for url in ("/cart/add", "/cart/items/service-assessment", "/cart/checkout"):
        assert client.post(url, data={}).status_code == 303
    _login_customer(client)
    for url in ("/cart/add", "/cart/items/service-assessment", "/cart/checkout"):
        assert client.post(url, data={}).status_code == 400
    assert web_context.repository.count_invoices() == 0
    assert web_context.wallet.calls == []


def test_cart_creates_one_owned_invoice_with_immutable_items(web_context):
    _second_service(web_context)
    _add(web_context, quantity=2)
    _add(web_context, "service-second")
    invoice, response, data = _checkout(web_context)

    assert invoice.price_usd_cents == 25_500
    assert invoice.expected_atomic == 1_275_000_000_000
    assert len(web_context.wallet.calls) == 1
    assert web_context.rate_provider.calls == 1
    assert web_context.repository.count_invoices() == 1
    assert not web_context.repository.get_cart(CUSTOMER_ID).items
    assert web_context.repository.get_customer_order(CUSTOMER_ID, invoice.id) == invoice
    items = web_context.repository.get_invoice_items(invoice.id)
    assert len(items) == 2
    assert sum(line.total_usd_cents for line in items) == invoice.price_usd_cents

    replay = web_context.client.post("/cart/checkout", data=data)
    assert replay.status_code == 400
    assert len(web_context.wallet.calls) == 1
    web_context.repository.update_service_price("service-second", price_usd_cents=1, now=NOW)
    assert web_context.repository.get_invoice_items(invoice.id) == items
    assert web_context.repository.get_invoice(invoice.id).price_usd_cents == 25_500
    for url in (response.headers["Location"], f"/account/orders/{invoice.id}"):
        page = web_context.client.get(url)
        body = page.get_data(as_text=True)
        assert "Second assessment" in body
        assert "$55.00 USD" in body
        assert page.headers["Cache-Control"] == "no-store, private, max-age=0"
        assert page.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    account_body = web_context.client.get("/account").get_data(as_text=True)
    assert 'class="order-history"' in account_body
    assert f'/account/orders/{invoice.id}' in account_body


def test_client_supplied_prices_and_owner_are_not_checkout_authority(web_context):
    _add(web_context, quantity=2)
    data = _tokens(web_context.client.get("/cart"))
    data.update({"total_usd_cents": "1", "quantity": "1", "customer_id": "another-customer-00001"})
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 303
    invoice = web_context.repository.get_invoice(response.headers["Location"].split("/")[2])
    assert invoice.price_usd_cents == 20_000
    assert web_context.repository.get_customer_order(CUSTOMER_ID, invoice.id) == invoice


def test_concurrent_claims_allow_only_one_checkout_worker(web_context):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    _add(web_context)
    repo = web_context.repository
    cart = repo.get_cart(CUSTOMER_ID)
    barrier = Barrier(2)
    def claim(index):
        barrier.wait(timeout=5)
        try:
            repo.claim_cart_checkout(CUSTOMER_ID, version=cart.version,
                fingerprint=cart.fingerprint, claim_token=f"concurrent-claim-{index}-00000001", now=NOW)
            return "claimed"
        except CheckoutInProgressError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(claim, [1, 2])) == ["blocked", "claimed"]


def test_orders_and_carts_are_isolated_between_customers(web_context):
    _add(web_context)
    invoice, response, _ = _checkout(web_context)
    other = web_context.app.test_client()
    token = _tokens(other.get("/register"))["csrf_token"]
    other.post("/register", data={
        "csrf_token": token, "username": "other.customer",
        "password": "a different strong password", "confirm_password": "a different strong password",
    })
    assert other.get(f"/account/orders/{invoice.id}").status_code == 404
    assert invoice.id not in other.get("/account").get_data(as_text=True)
    assert "Your cart is empty" in other.get("/cart").get_data(as_text=True)
    # Exact bearer links still require a signed-in site session.
    anonymous = web_context.app.test_client().get(response.headers["Location"])
    assert anonymous.status_code == 303
    assert "/login?next=" in anonymous.headers["Location"]


@pytest.mark.parametrize("change", ["price", "quantity", "archive"])
def test_stale_cart_review_is_rejected_before_rate_or_wallet_calls(web_context, change):
    _add(web_context)
    data = _tokens(web_context.client.get("/cart"))
    if change == "price":
        web_context.repository.update_service_price("service-assessment", price_usd_cents=5_000, now=NOW)
    elif change == "quantity":
        web_context.repository.change_cart_item(CUSTOMER_ID, "service-assessment", 2)
    else:
        web_context.repository.archive_service("service-assessment", now=NOW)
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 409
    assert web_context.wallet.calls == []
    assert web_context.rate_provider.calls == 0
    assert web_context.repository.count_invoices() == 0


def test_rate_failure_preserves_cart_and_releases_claim_for_retry(web_context):
    _add(web_context)
    data = _tokens(web_context.client.get("/cart"))
    web_context.rate_provider.error = XmrRateUnavailableError("private provider error")
    response = web_context.client.post("/cart/checkout", data=data)
    assert response.status_code == 503
    assert "private provider error" not in response.get_data(as_text=True)
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1
    assert web_context.wallet.calls == []
    web_context.rate_provider.error = None
    _checkout(web_context)


def test_replayed_original_session_cookie_cannot_create_another_invoice(web_context):
    _add(web_context)
    data = _tokens(web_context.client.get("/cart"))
    replay_client = web_context.app.test_client()
    with web_context.client.session_transaction() as original:
        saved_session = dict(original)
    with replay_client.session_transaction() as replay:
        replay.update(saved_session)
    assert web_context.client.post("/cart/checkout", data=data).status_code == 303
    assert replay_client.post("/cart/checkout", data=data).status_code == 409
    assert web_context.repository.count_invoices() == 1
    assert len(web_context.wallet.calls) == 1


def test_wallet_outage_preserves_cart_without_partial_order(web_context):
    _add(web_context)
    def unavailable_wallet(label):
        raise XmrWalletRpcError("private wallet failure")
    web_context.wallet.create_subaddress = unavailable_wallet
    response = web_context.client.post("/cart/checkout", data=_tokens(web_context.client.get("/cart")))
    assert response.status_code == 503
    assert "private wallet failure" not in response.get_data(as_text=True)
    assert web_context.repository.count_invoices() == 0
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1


def test_repeated_add_is_bounded_and_unavailable_items_can_be_removed(web_context):
    _add(web_context, quantity=10)
    token = _tokens(web_context.client.get("/cart"))["csrf_token"]
    assert web_context.client.post("/cart/add", data={
        "csrf_token": token, "service_id": "service-assessment", "quantity": "1"
    }).status_code == 400
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 10
    web_context.repository.archive_service("service-assessment", now=NOW)
    body = web_context.client.get("/cart").get_data(as_text=True)
    assert "no longer available" in body
    assert 'action="/cart/checkout"' not in body
    assert web_context.client.post("/cart/items/service-assessment", data={
        "csrf_token": token, "quantity": "0",
    }).status_code == 303
    assert web_context.repository.get_cart(CUSTOMER_ID).items == ()


def test_catalog_change_during_wallet_call_rolls_back_order(web_context):
    _add(web_context)
    original = web_context.wallet.create_subaddress
    def changed_catalog(label):
        web_context.repository.update_service_price("service-assessment", price_usd_cents=5_000, now=NOW)
        return original(label)
    web_context.wallet.create_subaddress = changed_catalog
    response = web_context.client.post("/cart/checkout", data=_tokens(web_context.client.get("/cart")))
    assert response.status_code == 409
    assert web_context.repository.count_invoices() == 0
    assert not web_context.repository.list_customer_orders(CUSTOMER_ID)
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1


def test_insert_failure_rolls_back_invoice_and_preserves_cart(web_context, monkeypatch):
    import app.persistence as persistence
    _add(web_context)
    original = persistence._insert_invoice_row
    def fail_after_insert(connection, invoice):
        original(connection, invoice)
        raise PersistenceError("private database error")
    monkeypatch.setattr(persistence, "_insert_invoice_row", fail_after_insert)
    response = web_context.client.post("/cart/checkout", data=_tokens(web_context.client.get("/cart")))
    assert response.status_code == 503
    assert web_context.repository.count_invoices() == 0
    assert not web_context.repository.list_customer_orders(CUSTOMER_ID)
    assert web_context.repository.get_cart(CUSTOMER_ID).quantity == 1


def test_sqlite_claim_prevents_overlapping_checkout_and_stale_lease_commit(web_context):
    _add(web_context)
    repo = web_context.repository
    cart = repo.get_cart(CUSTOMER_ID)
    first_token = "first-checkout-claim-00000001"
    second_token = "second-checkout-claim-0000001"
    lines = repo.claim_cart_checkout(CUSTOMER_ID, version=cart.version,
        fingerprint=cart.fingerprint, claim_token=first_token, now=NOW)
    with pytest.raises(CheckoutInProgressError):
        repo.claim_cart_checkout(CUSTOMER_ID, version=cart.version,
            fingerprint=cart.fingerprint, claim_token=second_token, now=NOW)
    repo.claim_cart_checkout(CUSTOMER_ID, version=cart.version,
        fingerprint=cart.fingerprint, claim_token=second_token, now=NOW + timedelta(minutes=6))
    creator = InvoiceCreator(repo, web_context.wallet, required_confirmations=10,
        sweep_required=False, now_factory=lambda: NOW)
    quote = web_context.rate_provider.get_quote()
    with pytest.raises(CartChangedError):
        creator.create_cart_invoice(CUSTOMER_ID, lines, quote,
            cart_version=cart.version, claim_token=first_token)
    assert repo.count_invoices() == 0
    creator.create_cart_invoice(CUSTOMER_ID, lines, quote,
        cart_version=cart.version, claim_token=second_token)
    assert repo.count_invoices() == 1


def test_admin_filters_include_every_order_line_and_fulfillment_stays_guarded(web_context):
    _second_service(web_context)
    _add(web_context)
    _add(web_context, "service-second")
    invoice, _, _ = _checkout(web_context)
    repo = web_context.repository
    assert repo.list_admin_purchases(service_id="service-second")[0].invoice_id == invoice.id
    assert repo.list_admin_purchases(category_id="category-second")[0].invoice_id == invoice.id
    assert not repo.list_admin_purchases(category_id="category-second", service_id="service-assessment")
    with pytest.raises(FulfillmentNotAllowedError):
        repo.mark_purchase_fulfilled(invoice.id, note="", now=NOW)
    repo.record_observation(invoice.id, observed_atomic=invoice.expected_atomic,
        observed_confirmations=10, deposit_txid="test-only", now=NOW)
    repo.transition_status(invoice.id, PaymentStatus.PAID_PENDING_CONFIRMATIONS, now=NOW)
    repo.transition_status(invoice.id, PaymentStatus.SETTLED, now=NOW)
    repo.mark_purchase_fulfilled(invoice.id, note="", now=NOW)
    assert "fulfillment has been marked complete" in web_context.client.get(
        f"/account/orders/{invoice.id}"
    ).get_data(as_text=True)
