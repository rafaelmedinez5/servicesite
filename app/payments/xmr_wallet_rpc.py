from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth


ATOMIC_UNITS_PER_XMR = Decimal("1000000000000")
MAX_ATOMIC_UNITS = (2**63) - 1
_XMR_INPUT_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?\Z")


class XmrAmountError(ValueError):
    """Raised when an XMR boundary value cannot be represented exactly."""


class XmrWalletRpcError(RuntimeError):
    """Base class for sanitized wallet-RPC failures."""


class XmrWalletRpcTransportError(XmrWalletRpcError):
    """The HTTP request could not complete after the configured attempts."""


class XmrWalletRpcHttpError(XmrWalletRpcError):
    """wallet-RPC returned a non-success HTTP status."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"XMR wallet-RPC HTTP status {status_code}")


class XmrWalletRpcProtocolError(XmrWalletRpcError):
    """wallet-RPC returned malformed JSON-RPC data."""


class XmrWalletRpcRemoteError(XmrWalletRpcError):
    """wallet-RPC returned a JSON-RPC error without exposing its message."""

    def __init__(self, code: int | None) -> None:
        self.code = code
        label = str(code) if code is not None else "unknown"
        super().__init__(f"XMR wallet-RPC returned error code {label}")


def xmr_to_atomic(value: str | Decimal) -> int:
    """Convert a positive XMR boundary value to exact integer atomic units."""

    if isinstance(value, str):
        raw_value = value.strip()
        if not _XMR_INPUT_PATTERN.fullmatch(raw_value):
            raise XmrAmountError("XMR amount must be a plain positive decimal with at most 12 places")
        try:
            amount = Decimal(raw_value)
        except InvalidOperation as exc:
            raise XmrAmountError("XMR amount is invalid") from exc
    elif isinstance(value, Decimal):
        amount = value
    else:
        raise XmrAmountError("XMR amount must be supplied as a string or Decimal")

    if not amount.is_finite() or amount <= 0:
        raise XmrAmountError("XMR amount must be finite and greater than zero")

    atomic_value = amount * ATOMIC_UNITS_PER_XMR
    if atomic_value != atomic_value.to_integral_value():
        raise XmrAmountError("XMR amount has more than 12 decimal places")

    atomic = int(atomic_value)
    if atomic > MAX_ATOMIC_UNITS:
        raise XmrAmountError("XMR amount exceeds the supported storage range")
    return atomic


def atomic_to_xmr_str(atomic: int) -> str:
    """Return a numeric XMR string without a currency suffix."""

    if isinstance(atomic, bool) or not isinstance(atomic, int):
        raise XmrAmountError("Atomic amount must be an integer")
    if not 0 <= atomic <= MAX_ATOMIC_UNITS:
        raise XmrAmountError("Atomic amount is outside the supported storage range")
    amount = Decimal(atomic) / ATOMIC_UNITS_PER_XMR
    return format(amount, ".12f")


@dataclass(frozen=True)
class XmrSubaddress:
    address: str
    account_index: int
    address_index: int


@dataclass(frozen=True)
class WalletRpcConfig:
    url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    account_index: int = 0
    timeout_seconds: float = 20.0
    max_attempts: int = 3
    retry_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("wallet-RPC URL must be an HTTP(S) URL with a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("wallet-RPC credentials must not be embedded in the URL")
        if parsed.query or parsed.fragment:
            raise ValueError("wallet-RPC URL must not contain a query string or fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("wallet-RPC URL has an invalid port") from exc
        if not self.username or not self.password:
            raise ValueError("wallet-RPC digest username and password are required")
        if (
            isinstance(self.account_index, bool)
            or not isinstance(self.account_index, int)
            or self.account_index < 0
        ):
            raise ValueError("wallet-RPC account index must be a non-negative integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0 < self.timeout_seconds <= 300
        ):
            raise ValueError("wallet-RPC timeout must be between 0 and 300 seconds")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ValueError("wallet-RPC max attempts must be between 1 and 10")
        if (
            isinstance(self.retry_backoff_seconds, bool)
            or not isinstance(self.retry_backoff_seconds, (int, float))
            or not 0 <= self.retry_backoff_seconds <= 60
        ):
            raise ValueError("wallet-RPC retry backoff must be between 0 and 60 seconds")


class HttpSession(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...


class XmrWalletRpcClient:
    """Small JSON-RPC transport with injected HTTP and sleep dependencies."""

    def __init__(
        self,
        config: WalletRpcConfig,
        *,
        session: HttpSession | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.auth = HTTPDigestAuth(config.username, config.password)

    def rpc_call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not method:
            raise ValueError("wallet-RPC method is required")
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}

        for attempt in range(1, self.config.max_attempts + 1):
            try:
                response = self.session.post(
                    self.config.url,
                    json=payload,
                    timeout=self.config.timeout_seconds,
                    auth=self.auth,
                )
            except requests.RequestException as exc:
                if attempt < self.config.max_attempts:
                    self._backoff(attempt)
                    continue
                raise XmrWalletRpcTransportError(
                    "XMR wallet-RPC request failed after bounded attempts"
                ) from exc

            status_code = getattr(response, "status_code", None)
            if isinstance(status_code, bool) or not isinstance(status_code, int):
                raise XmrWalletRpcProtocolError("XMR wallet-RPC response has no valid HTTP status")
            if not 200 <= status_code < 300:
                http_error = XmrWalletRpcHttpError(status_code)
                if (status_code == 429 or status_code >= 500) and attempt < self.config.max_attempts:
                    self._backoff(attempt)
                    continue
                raise http_error

            try:
                data = response.json()
            except (TypeError, ValueError) as exc:
                raise XmrWalletRpcProtocolError("XMR wallet-RPC response is not valid JSON") from exc

            if not isinstance(data, dict):
                raise XmrWalletRpcProtocolError("XMR wallet-RPC response must be a JSON object")
            if data.get("error") is not None:
                error = data["error"]
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, bool) or not isinstance(code, int):
                    code = None
                raise XmrWalletRpcRemoteError(code)
            result = data.get("result")
            if not isinstance(result, dict):
                raise XmrWalletRpcProtocolError("XMR wallet-RPC response has no result object")
            return result

        raise AssertionError("wallet-RPC attempt loop ended unexpectedly")

    def create_address(self, account_index: int, label: str) -> tuple[str, int]:
        _require_non_negative_int(account_index, "account index")
        result = self.rpc_call(
            "create_address", {"account_index": account_index, "label": str(label)}
        )
        address = result.get("address")
        address_index = result.get("address_index")
        if not isinstance(address, str) or not address:
            raise XmrWalletRpcProtocolError("create_address returned no address")
        _require_non_negative_int(address_index, "address index", protocol_error=True)
        return address, address_index

    def create_subaddress(self, label: str) -> XmrSubaddress:
        address, address_index = self.create_address(self.config.account_index, label)
        return XmrSubaddress(
            address=address,
            account_index=self.config.account_index,
            address_index=address_index,
        )

    def get_height(self) -> int:
        height = self.rpc_call("get_height").get("height")
        _require_non_negative_int(height, "wallet height", protocol_error=True)
        return height

    def get_transfers_in(self, account_index: int) -> list[dict[str, Any]]:
        _require_non_negative_int(account_index, "account index")
        result = self.rpc_call("get_transfers", {"in": True, "account_index": account_index})
        return _require_transfer_list(result.get("in", []))

    def get_transfer_by_txid(
        self, txid: str, account_index: int | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(txid, str) or not txid:
            raise ValueError("transaction ID is required")
        params: dict[str, Any] = {"txid": txid}
        if account_index is not None:
            _require_non_negative_int(account_index, "account index")
            params["account_index"] = account_index
        result = self.rpc_call("get_transfer_by_txid", params)
        return _require_transfer_list(result.get("transfers", []))

    def sweep_all(
        self,
        *,
        address: str,
        account_index: int,
        priority: int = 2,
        relay: bool = True,
        subaddr_indices: list[int] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(address, str) or not address:
            raise ValueError("sweep destination is required")
        _require_non_negative_int(account_index, "account index")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 4:
            raise ValueError("sweep priority must be an integer from 0 through 4")
        if not isinstance(relay, bool):
            raise ValueError("sweep relay must be a boolean")

        params: dict[str, Any] = {
            "address": address,
            "account_index": account_index,
            "priority": priority,
            "relay": relay,
        }
        if subaddr_indices is not None:
            for address_index in subaddr_indices:
                _require_non_negative_int(address_index, "subaddress index")
            params["subaddr_indices"] = list(subaddr_indices)
        return self.rpc_call("sweep_all", params)

    def _backoff(self, attempt: int) -> None:
        self.sleeper(self.config.retry_backoff_seconds * attempt)


def _require_non_negative_int(
    value: Any, label: str, *, protocol_error: bool = False
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        if protocol_error:
            raise XmrWalletRpcProtocolError(f"wallet-RPC returned an invalid {label}")
        raise ValueError(f"{label} must be a non-negative integer")


def _require_transfer_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise XmrWalletRpcProtocolError("wallet-RPC returned an invalid transfer list")
    return value
