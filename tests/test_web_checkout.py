from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import create_app
from app.catalog import CategoryRecord, ServiceRecord
from app.payments.invoice import PaymentStatus, XmrQuote
from app.payments.xmr_rate import XmrRateUnavailableError
from app.payments.xmr_wallet_rpc import XmrSubaddress
from app.persistence import SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
TOKEN_PATTERN = re.compile(r'name="(?P<name>csrf_token|checkout_nonce)" value="(?P<value>[^"]+)"')


class FakeWallet:
    def __init__(self):
        self.calls = []

    def create_subaddress(self, label):
        self.calls.append(label)
        index = len(self.calls)
        return XmrSubaddress(
            address="4" + (str(index % 10) * 94),
            account_index=7,
            address_index=index,
        )


class FakeRateProvider:
    def __init__(self, error=None, *, quote_age=timedelta(seconds=30)):
        self.error = error
        self.quote_age = quote_age
        self.calls = 0

    def get_quote(self):
        self.calls += 1
        if self.error:
            raise self.error
        return XmrQuote(
            usd_per_xmr=Decimal("200"),
            source="test-only-rate-source",
            quoted_at=NOW - self.quote_age,
        )


@dataclass
class WebContext:
    app: object
    client: object
    repository: ServicesiteRepository
    wallet: FakeWallet
    rate_provider: FakeRateProvider


@pytest.fixture
def web_context(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key")
    database = SQLiteDatabase(tmp_path / "web.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.insert_category(_category())
    repository.insert_service(_service())
    wallet = FakeWallet()
    rate_provider = FakeRateProvider()
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "web.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_WALLET_CLIENT": wallet,
            "SERVICESITE_RATE_PROVIDER": rate_provider,
            "SERVICESITE_NOW_FACTORY": lambda: NOW,
            "XMR_SWEEP_ENABLED": False,
        }
    )
    return WebContext(
        app=app,
        client=app.test_client(),
        repository=repository,
        wallet=wallet,
        rate_provider=rate_provider,
    )


def _category():
    return CategoryRecord(
        id="category-security",
        name="Security Services",
        slug="security-services",
        description="Authorized security engagements.",
        published=True,
        archived=False,
        sort_order=10,
        created_at=NOW,
        updated_at=NOW,
    )


def _service(*, service_id="service-assessment", published=True):
    return ServiceRecord(
        id=service_id,
        category_id="category-security",
        name="Security Assessment",
        slug=f"{service_id}-slug",
        description="Authorized assessment with a written report.",
        price_usd_cents=10_000,
        duration_label="One engagement",
        published=published,
        archived=False,
        sort_order=10,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _form_tokens(client):
    response = client.get("/")
    assert response.status_code == 200
    tokens = {match.group("name"): match.group("value") for match in TOKEN_PATTERN.finditer(response.get_data(as_text=True))}
    assert set(tokens) == {"csrf_token", "checkout_nonce"}
    return tokens


def _create_invoice(context):
    data = _form_tokens(context.client)
    data["service_id"] = "service-assessment"
    response = context.client.post("/checkout", data=data)
    assert response.status_code == 303
    invoice = context.repository.get_invoice(response.headers["Location"].split("/")[2])
    assert invoice is not None
    return invoice, response


def _private_urls(invoice):
    base = f"/checkout/{invoice.id}/{invoice.status_token}"
    return {
        "checkout": base,
        "qr": f"{base}/qr.svg",
        "status": f"/status/{invoice.id}/{invoice.status_token}",
    }


def test_public_catalog_renders_only_published_services(web_context):
    web_context.repository.insert_service(
        _service(service_id="service-hidden", published=False)
    )

    response = web_context.client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Security Assessment" in body
    assert "service-hidden" not in body
    assert "<script" not in body.lower()


def test_csrf_and_service_validation_happen_before_invoice_creation(web_context):
    missing_csrf = web_context.client.post(
        "/checkout",
        data={"service_id": "service-assessment", "checkout_nonce": "invalid"},
    )
    tokens = _form_tokens(web_context.client)
    tokens["service_id"] = "service-does-not-exist"
    invalid_service = web_context.client.post("/checkout", data=tokens)

    assert missing_csrf.status_code == 400
    assert invalid_service.status_code == 400
    assert web_context.repository.count_invoices() == 0
    assert web_context.rate_provider.calls == 0
    assert web_context.wallet.calls == []


def test_successful_submit_creates_one_invoice_and_nonce_cannot_be_replayed(web_context):
    data = _form_tokens(web_context.client)
    data["service_id"] = "service-assessment"

    first = web_context.client.post("/checkout", data=data)
    replay = web_context.client.post("/checkout", data=data)

    assert first.status_code == 303
    assert replay.status_code == 400
    assert web_context.repository.count_invoices() == 1
    assert web_context.rate_provider.calls == 1
    assert len(web_context.wallet.calls) == 1


def test_unavailable_rate_fails_without_wallet_or_invoice(web_context):
    web_context.app.extensions["servicesite_rate_provider"] = FakeRateProvider(
        XmrRateUnavailableError("test-only provider outage")
    )
    data = _form_tokens(web_context.client)
    data["service_id"] = "service-assessment"

    response = web_context.client.post("/checkout", data=data)

    assert response.status_code == 503
    assert "provider outage" not in response.get_data(as_text=True)
    assert web_context.repository.count_invoices() == 0
    assert web_context.wallet.calls == []


def test_stale_injected_quote_is_rechecked_before_wallet_or_invoice(web_context):
    web_context.app.extensions["servicesite_rate_provider"] = FakeRateProvider(
        quote_age=timedelta(seconds=301)
    )
    data = _form_tokens(web_context.client)
    data["service_id"] = "service-assessment"

    response = web_context.client.post("/checkout", data=data)

    assert response.status_code == 503
    assert web_context.repository.count_invoices() == 0
    assert web_context.wallet.calls == []


def test_checkout_qr_and_status_require_exact_bearer_token(web_context):
    invoice, _ = _create_invoice(web_context)
    urls = _private_urls(invoice)

    for url in urls.values():
        valid = web_context.client.get(url)
        invalid = web_context.client.get(url.replace(invoice.status_token, "wrong-token"))
        assert valid.status_code == 200
        assert invalid.status_code == 404


def test_checkout_contains_numeric_monero_uri_and_customer_safe_fields(web_context):
    invoice, _ = _create_invoice(web_context)
    response = web_context.client.get(_private_urls(invoice)["checkout"])
    body = response.get_data(as_text=True)

    assert f"monero:{invoice.xmr_address}?tx_amount=0.500000000000" in body
    assert "tx_amount=0.500000000000+XMR" not in body
    assert "0.500000000000 XMR" in body
    assert invoice.id in body
    assert invoice.xmr_address in body
    assert "xmr_account_index" not in body
    assert "xmr_address_index" not in body
    assert "deposit_txid" not in body
    assert "sweep_txid" not in body


def test_qr_is_svg_for_payment_uri_and_does_not_embed_status_token(web_context):
    invoice, _ = _create_invoice(web_context)
    response = web_context.client.get(_private_urls(invoice)["qr"])

    assert response.status_code == 200
    assert response.mimetype == "image/svg+xml"
    assert response.get_data().lstrip().startswith(b"<svg")
    assert invoice.status_token.encode() not in response.get_data()


def test_private_resources_have_no_store_noindex_and_security_headers(web_context):
    invoice, redirect_response = _create_invoice(web_context)
    responses = [
        redirect_response,
        *(web_context.client.get(url) for url in _private_urls(invoice).values()),
    ]

    for response in responses:
        assert response.headers["Cache-Control"] == "no-store, private, max-age=0"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "script-src 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Frame-Options"] == "DENY"


def test_partial_pending_expired_and_settled_customer_states(web_context):
    partial, _ = _create_invoice(web_context)
    web_context.repository.record_observation(
        partial.id,
        observed_atomic=partial.expected_atomic // 2,
        observed_confirmations=0,
        deposit_txid="test-only-partial",
        now=NOW + timedelta(minutes=1),
    )
    partial_body = web_context.client.get(_private_urls(partial)["status"]).get_data(as_text=True)

    pending, _ = _create_invoice(web_context)
    web_context.repository.record_observation(
        pending.id,
        observed_atomic=pending.expected_atomic,
        observed_confirmations=3,
        deposit_txid="test-only-pending",
        now=NOW + timedelta(minutes=1),
    )
    web_context.repository.transition_status(
        pending.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    pending_body = web_context.client.get(_private_urls(pending)["status"]).get_data(as_text=True)

    expired, _ = _create_invoice(web_context)
    web_context.repository.transition_status(
        expired.id,
        PaymentStatus.EXPIRED,
        now=expired.expires_at,
    )
    expired_body = web_context.client.get(_private_urls(expired)["status"]).get_data(as_text=True)

    settled, _ = _create_invoice(web_context)
    web_context.repository.record_observation(
        settled.id,
        observed_atomic=settled.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-settled",
        now=NOW + timedelta(minutes=1),
    )
    web_context.repository.transition_status(
        settled.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    web_context.repository.transition_status(
        settled.id,
        PaymentStatus.SETTLED,
        now=NOW + timedelta(minutes=2),
    )
    settled_body = web_context.client.get(_private_urls(settled)["status"]).get_data(as_text=True)

    assert "Partial payment received" in partial_body
    assert "Service fulfillment has not started" in partial_body
    assert "Payment detected" in pending_body
    assert "3 of 10 confirmations" in pending_body
    assert "Service fulfillment has not started" in pending_body
    assert "Payment window expired" in expired_body
    assert "Service fulfillment has not started" in expired_body
    assert "Payment settled" in settled_body
    assert "eligible for manual fulfillment review" in settled_body
    for body in (partial_body, pending_body, expired_body, settled_body):
        assert "paid_pending" not in body
        assert "sweeping_to_cold" not in body
        assert "test-only-" not in body
