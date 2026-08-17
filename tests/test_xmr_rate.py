from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests

from app.payments.xmr_rate import (
    COINGECKO_DEMO_PRICE_URL,
    CoinGeckoRateClient,
    CoinGeckoRateConfig,
    XmrRateStaleError,
    XmrRateUnavailableError,
)


NOW = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self.status_code = status_code
        self.content = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        )


class FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _payload(*, rate="123.456789012345", updated_at=None):
    return (
        '{"monero":{"usd":'
        + rate
        + ',"last_updated_at":'
        + str(updated_at or int((NOW - timedelta(seconds=30)).timestamp()))
        + "}}"
    ).encode("utf-8")


def _client(outcome, **config_overrides):
    values = {
        "api_key": "test-only-coingecko-key",
        "timeout_seconds": 4,
        "maximum_age": timedelta(minutes=5),
    }
    values.update(config_overrides)
    session = FakeSession(outcome)
    return (
        CoinGeckoRateClient(
            CoinGeckoRateConfig(**values),
            session=session,
            now_factory=lambda: NOW,
        ),
        session,
    )


def test_coingecko_quote_preserves_exact_decimal_and_provider_timestamp():
    client, session = _client(FakeResponse(_payload()))

    quote = client.get_quote()

    assert quote.usd_per_xmr == Decimal("123.456789012345")
    assert quote.rate_text == "123.456789012345"
    assert quote.quoted_at == NOW - timedelta(seconds=30)
    call = session.calls[0]
    assert call["url"] == COINGECKO_DEMO_PRICE_URL
    assert call["timeout"] == 4.0
    assert call["headers"]["x-cg-demo-api-key"] == "test-only-coingecko-key"
    assert call["params"] == {
        "ids": "monero",
        "vs_currencies": "usd",
        "include_last_updated_at": "true",
        "precision": "full",
    }


def test_quote_older_than_five_minutes_is_rejected():
    stale_at = int((NOW - timedelta(seconds=301)).timestamp())
    client, _ = _client(FakeResponse(_payload(updated_at=stale_at)))

    with pytest.raises(XmrRateStaleError, match="stale"):
        client.get_quote()


@pytest.mark.parametrize(
    "outcome",
    [
        requests.Timeout("test-only timeout"),
        FakeResponse({}, status_code=429),
        FakeResponse(b"not-json"),
        FakeResponse(b"{}"),
        FakeResponse(_payload(rate="0")),
        FakeResponse(_payload(rate="NaN")),
    ],
)
def test_provider_outage_and_malformed_values_fail_safely(outcome):
    client, _ = _client(outcome)

    with pytest.raises(XmrRateUnavailableError):
        client.get_quote()


def test_missing_api_key_fails_before_network_access():
    client, session = _client(FakeResponse(_payload()), api_key="")

    with pytest.raises(XmrRateUnavailableError, match="not configured"):
        client.get_quote()
    assert session.calls == []


def test_rate_configuration_repr_hides_api_key():
    rendered = repr(CoinGeckoRateConfig(api_key="test-only-secret-key"))

    assert "test-only-secret-key" not in rendered
