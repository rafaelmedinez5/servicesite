from __future__ import annotations

from decimal import Decimal

import pytest
import requests
from requests.auth import HTTPDigestAuth

from app.payments.xmr_wallet_rpc import (
    MAX_ATOMIC_UNITS,
    WalletRpcConfig,
    XmrAmountError,
    XmrWalletRpcClient,
    XmrWalletRpcHttpError,
    XmrWalletRpcProtocolError,
    XmrWalletRpcRemoteError,
    XmrWalletRpcTransportError,
    atomic_to_xmr_str,
    xmr_to_atomic,
)


class FakeResponse:
    def __init__(self, payload=None, *, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def rpc_config(**overrides):
    values = {
        "url": "http://127.0.0.1:28088/json_rpc",
        "username": "test-only-user",
        "password": "test-only-password",
        "account_index": 7,
        "timeout_seconds": 4.0,
        "max_attempts": 3,
        "retry_backoff_seconds": 0.25,
    }
    values.update(overrides)
    return WalletRpcConfig(**values)


@pytest.mark.parametrize(
    "override",
    [
        {"url": "http://user:password@127.0.0.1:28088/json_rpc"},
        {"url": "http://127.0.0.1:28088/json_rpc?secret=not-allowed"},
        {"username": ""},
        {"password": ""},
        {"account_index": True},
        {"timeout_seconds": "20"},
        {"max_attempts": 0},
        {"max_attempts": "3"},
        {"retry_backoff_seconds": True},
    ],
)
def test_wallet_rpc_config_rejects_unsafe_or_malformed_values(override):
    with pytest.raises(ValueError):
        rpc_config(**override)


def test_wallet_rpc_config_repr_hides_digest_credentials():
    rendered = repr(rpc_config())
    assert "test-only-user" not in rendered
    assert "test-only-password" not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", 1_000_000_000_000),
        ("0.000000000001", 1),
        ("12.345678901234", 12_345_678_901_234),
        (Decimal("0.5"), 500_000_000_000),
    ],
)
def test_xmr_to_atomic_is_exact(value, expected):
    assert xmr_to_atomic(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "-1",
        "+1",
        "01",
        "1e-12",
        "0.0000000000001",
        "not-a-number",
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-0.1"),
        1,
        0.1,
        True,
    ],
)
def test_xmr_to_atomic_rejects_invalid_boundary_values(value):
    with pytest.raises(XmrAmountError):
        xmr_to_atomic(value)


def test_xmr_to_atomic_rejects_values_outside_sqlite_integer_range():
    too_large = (Decimal(MAX_ATOMIC_UNITS) + 1) / Decimal("1000000000000")
    with pytest.raises(XmrAmountError, match="storage range"):
        xmr_to_atomic(too_large)


def test_atomic_format_is_numeric_and_has_no_currency_suffix():
    assert atomic_to_xmr_str(100_000_000_001) == "0.100000000001"
    assert "XMR" not in atomic_to_xmr_str(1)


def test_digest_auth_and_timeout_are_attached():
    session = FakeSession([FakeResponse({"result": {"height": 123}})])
    client = XmrWalletRpcClient(rpc_config(), session=session, sleeper=lambda _: None)

    assert client.get_height() == 123
    call = session.calls[0]
    assert call["timeout"] == 4.0
    assert isinstance(call["auth"], HTTPDigestAuth)
    assert call["auth"].username == "test-only-user"
    assert call["auth"].password == "test-only-password"


def test_retry_then_success_uses_linear_backoff():
    session = FakeSession(
        [
            requests.Timeout("test timeout"),
            FakeResponse({"result": {"height": 456}}),
        ]
    )
    sleeps = []
    client = XmrWalletRpcClient(
        rpc_config(max_attempts=2, retry_backoff_seconds=0.5),
        session=session,
        sleeper=sleeps.append,
    )

    assert client.get_height() == 456
    assert len(session.calls) == 2
    assert sleeps == [0.5]


def test_transport_failure_is_bounded():
    session = FakeSession([requests.ConnectionError("one"), requests.Timeout("two")])
    client = XmrWalletRpcClient(
        rpc_config(max_attempts=2), session=session, sleeper=lambda _: None
    )

    with pytest.raises(XmrWalletRpcTransportError, match="bounded attempts"):
        client.get_height()
    assert len(session.calls) == 2


def test_retryable_http_failure_is_bounded():
    session = FakeSession(
        [FakeResponse(status_code=503), FakeResponse(status_code=503)]
    )
    client = XmrWalletRpcClient(
        rpc_config(max_attempts=2), session=session, sleeper=lambda _: None
    )

    with pytest.raises(XmrWalletRpcHttpError) as caught:
        client.get_height()
    assert caught.value.status_code == 503
    assert len(session.calls) == 2


def test_json_rpc_error_is_typed_and_not_retried():
    session = FakeSession(
        [FakeResponse({"error": {"code": -4, "message": "test-only remote detail"}})]
    )
    client = XmrWalletRpcClient(rpc_config(), session=session, sleeper=lambda _: None)

    with pytest.raises(XmrWalletRpcRemoteError) as caught:
        client.get_height()
    assert caught.value.code == -4
    assert "remote detail" not in str(caught.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(json_error=ValueError("invalid json")),
        FakeResponse([]),
        FakeResponse({}),
        FakeResponse({"result": []}),
        FakeResponse({"result": {"height": "123"}}),
    ],
)
def test_malformed_responses_are_rejected(response):
    client = XmrWalletRpcClient(
        rpc_config(), session=FakeSession([response]), sleeper=lambda _: None
    )
    with pytest.raises(XmrWalletRpcProtocolError):
        client.get_height()


def test_subaddress_result_is_parsed_with_both_indexes():
    session = FakeSession(
        [FakeResponse({"result": {"address": "test-only-subaddress", "address_index": 9}})]
    )
    client = XmrWalletRpcClient(rpc_config(account_index=7), session=session)

    subaddress = client.create_subaddress("invoice:test")

    assert subaddress.address == "test-only-subaddress"
    assert subaddress.account_index == 7
    assert subaddress.address_index == 9
    assert session.calls[0]["json"]["params"] == {
        "account_index": 7,
        "label": "invoice:test",
    }


def test_transfer_and_sweep_methods_preserve_required_parameters():
    session = FakeSession(
        [
            FakeResponse({"result": {"in": [{"amount": 1}]}}),
            FakeResponse({"result": {"transfers": [{"confirmations": 2}]}}),
            FakeResponse({"result": {"tx_hash_list": ["test-only-tx"]}}),
        ]
    )
    client = XmrWalletRpcClient(rpc_config(), session=session)

    assert client.get_transfers_in(7) == [{"amount": 1}]
    assert client.get_transfer_by_txid("test-only-deposit", 7) == [{"confirmations": 2}]
    assert client.sweep_all(
        address="test-only-cold-destination",
        account_index=7,
        priority=2,
        relay=False,
        subaddr_indices=[9],
    ) == {"tx_hash_list": ["test-only-tx"]}

    assert session.calls[2]["json"]["params"] == {
        "address": "test-only-cold-destination",
        "account_index": 7,
        "priority": 2,
        "relay": False,
        "subaddr_indices": [9],
    }
