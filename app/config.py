from __future__ import annotations

import ipaddress
import math
import os
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PLACEHOLDER_SECRET_VALUES = {
    "",
    "change-me",
    "changeme",
    "development-only-not-for-production",
    "replace_me",
    "replace_only_when_sweeping_is_approved",
    "replace_with_a_strong_random_token",
    "replace_with_at_least_32_random_characters",
    "replace_with_rpc_password",
    "replace_with_rpc_username",
}


def _parse_int(name: str, raw_value: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_float(name: str, raw_value: str, *, minimum: float, maximum: float) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _parse_bool(name: str, raw_value: str) -> bool:
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _parse_http_url(name: str, raw_value: str) -> SplitResult:
    value = raw_value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{name} must be an HTTP(S) URL with a host")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError(f"{name} must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must not contain a query string or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} has an invalid port") from exc
    return parsed


def _is_loopback_or_private(parsed_url: SplitResult) -> bool:
    hostname = (parsed_url.hostname or "").lower()
    if hostname == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_SECRET_VALUES


def _require_production_secret(name: str, value: str, *, minimum_length: int) -> None:
    if _is_placeholder(value) or len(value) < minimum_length:
        raise RuntimeError(
            f"Production {name} must be explicit, non-placeholder, and at least "
            f"{minimum_length} characters"
        )


@dataclass(frozen=True)
class Settings:
    environment: str
    secret_key: str = field(repr=False)
    app_host: str
    app_port: int
    database_path: str
    xmr_wallet_rpc_url: str
    xmr_wallet_rpc_user: str = field(repr=False)
    xmr_wallet_rpc_password: str = field(repr=False)
    xmr_account_index: int
    xmr_rpc_timeout_seconds: float
    xmr_rpc_max_attempts: int
    xmr_rpc_retry_backoff_seconds: float
    xmr_min_confirmations: int
    xmr_sweep_enabled: bool
    xmr_cold_address: str = field(repr=False)
    xmr_sweep_account_index: int
    xmr_sweep_priority: int
    xmr_sweep_relay: bool
    internal_token: str = field(repr=False)
    allow_public_xmr_wallet_rpc: bool

    @classmethod
    def from_env(cls) -> "Settings":
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise RuntimeError("ENVIRONMENT must be development, test, or production")

        secret_key = os.getenv("SECRET_KEY", "development-only-not-for-production").strip()
        app_host = os.getenv("APP_HOST", "127.0.0.1").strip()
        if app_host not in LOOPBACK_HOSTS:
            raise RuntimeError("APP_HOST must remain loopback-only")

        database_path = os.getenv(
            "DB_PATH", "/opt/servicesite/instance/servicesite.db"
        ).strip()
        if not database_path:
            raise RuntimeError("DB_PATH is required")

        xmr_wallet_rpc_url = os.getenv(
            "XMR_WALLET_RPC_URL", "http://127.0.0.1:28088/json_rpc"
        ).strip()
        parsed_rpc_url = _parse_http_url("XMR_WALLET_RPC_URL", xmr_wallet_rpc_url)
        xmr_wallet_rpc_user = os.getenv("XMR_WALLET_RPC_USER", "").strip()
        xmr_wallet_rpc_password = os.getenv("XMR_WALLET_RPC_PASS", "").strip()
        allow_public_xmr_wallet_rpc = _parse_bool(
            "ALLOW_PUBLIC_XMR_WALLET_RPC",
            os.getenv("ALLOW_PUBLIC_XMR_WALLET_RPC", "false"),
        )
        xmr_sweep_enabled = _parse_bool(
            "XMR_SWEEP_ENABLED", os.getenv("XMR_SWEEP_ENABLED", "false")
        )
        xmr_cold_address = os.getenv("XMR_COLD_ADDRESS", "").strip()
        internal_token = os.getenv("X_INTERNAL_TOKEN", "").strip()

        if environment == "production":
            _require_production_secret("SECRET_KEY", secret_key, minimum_length=32)
            _require_production_secret(
                "XMR_WALLET_RPC_USER", xmr_wallet_rpc_user, minimum_length=1
            )
            _require_production_secret(
                "XMR_WALLET_RPC_PASS", xmr_wallet_rpc_password, minimum_length=16
            )
            _require_production_secret("X_INTERNAL_TOKEN", internal_token, minimum_length=32)
            if not _is_loopback_or_private(parsed_rpc_url) and not allow_public_xmr_wallet_rpc:
                raise RuntimeError(
                    "Production XMR_WALLET_RPC_URL must use loopback/private networking; "
                    "ALLOW_PUBLIC_XMR_WALLET_RPC is a dangerous explicit override"
                )
            if xmr_sweep_enabled and _is_placeholder(xmr_cold_address):
                raise RuntimeError(
                    "Production XMR_COLD_ADDRESS is required when XMR_SWEEP_ENABLED is true"
                )

        return cls(
            environment=environment,
            secret_key=secret_key,
            app_host=app_host,
            app_port=_parse_int(
                "APP_PORT", os.getenv("APP_PORT", "5100").strip(), minimum=1, maximum=65535
            ),
            database_path=database_path,
            xmr_wallet_rpc_url=xmr_wallet_rpc_url,
            xmr_wallet_rpc_user=xmr_wallet_rpc_user,
            xmr_wallet_rpc_password=xmr_wallet_rpc_password,
            xmr_account_index=_parse_int(
                "XMR_ACCOUNT_INDEX",
                os.getenv("XMR_ACCOUNT_INDEX", "0").strip(),
                minimum=0,
                maximum=2**31 - 1,
            ),
            xmr_rpc_timeout_seconds=_parse_float(
                "XMR_RPC_TIMEOUT",
                os.getenv("XMR_RPC_TIMEOUT", "20").strip(),
                minimum=0.1,
                maximum=300,
            ),
            xmr_rpc_max_attempts=_parse_int(
                "XMR_RPC_RETRIES",
                os.getenv("XMR_RPC_RETRIES", "3").strip(),
                minimum=1,
                maximum=10,
            ),
            xmr_rpc_retry_backoff_seconds=_parse_float(
                "XMR_RPC_RETRY_BACKOFF",
                os.getenv("XMR_RPC_RETRY_BACKOFF", "1.0").strip(),
                minimum=0,
                maximum=60,
            ),
            xmr_min_confirmations=_parse_int(
                "XMR_MIN_CONFIRMATIONS",
                os.getenv("XMR_MIN_CONFIRMATIONS", "10").strip(),
                minimum=1,
                maximum=1000,
            ),
            xmr_sweep_enabled=xmr_sweep_enabled,
            xmr_cold_address=xmr_cold_address,
            xmr_sweep_account_index=_parse_int(
                "XMR_SWEEP_ACCOUNT_INDEX",
                os.getenv("XMR_SWEEP_ACCOUNT_INDEX", "0").strip(),
                minimum=0,
                maximum=2**31 - 1,
            ),
            xmr_sweep_priority=_parse_int(
                "XMR_SWEEP_PRIORITY",
                os.getenv("XMR_SWEEP_PRIORITY", "2").strip(),
                minimum=0,
                maximum=4,
            ),
            xmr_sweep_relay=_parse_bool(
                "XMR_SWEEP_RELAY", os.getenv("XMR_SWEEP_RELAY", "true")
            ),
            internal_token=internal_token,
            allow_public_xmr_wallet_rpc=allow_public_xmr_wallet_rpc,
        )
