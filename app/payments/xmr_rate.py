from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Protocol

import requests

from app.payments.invoice import XmrQuote


COINGECKO_DEMO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_RATE_SOURCE = "coingecko-demo:monero/usd"
MAX_RESPONSE_BYTES = 64 * 1024


class XmrRateError(RuntimeError):
    """A customer-safe base error for unavailable or invalid pricing data."""


class XmrRateUnavailableError(XmrRateError):
    """The approved provider could not supply a usable quote."""


class XmrRateStaleError(XmrRateError):
    """The approved provider supplied a quote outside the freshness policy."""


class HttpResponse(Protocol):
    status_code: int
    content: bytes


class HttpSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...


@dataclass(frozen=True)
class CoinGeckoRateConfig:
    api_key: str = field(repr=False)
    timeout_seconds: float = 10.0
    maximum_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str):
            raise ValueError("CoinGecko API key must be text")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 0.1 <= self.timeout_seconds <= 30
        ):
            raise ValueError("rate timeout must be between 0.1 and 30 seconds")
        if not isinstance(self.maximum_age, timedelta) or not (
            timedelta(seconds=30) <= self.maximum_age <= timedelta(minutes=5)
        ):
            raise ValueError("maximum quote age must be between 30 seconds and 5 minutes")


class CoinGeckoRateClient:
    """Fetch an exact, timestamped Monero/USD quote from CoinGecko Demo."""

    def __init__(
        self,
        config: CoinGeckoRateConfig,
        *,
        session: HttpSession | None = None,
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.now_factory = now_factory

    def get_quote(self) -> XmrQuote:
        if not self.config.api_key.strip():
            raise XmrRateUnavailableError("approved XMR pricing is not configured")

        now = self.now_factory()
        _require_aware_datetime(now)
        try:
            response = self.session.get(
                COINGECKO_DEMO_PRICE_URL,
                params={
                    "ids": "monero",
                    "vs_currencies": "usd",
                    "include_last_updated_at": "true",
                    "precision": "full",
                },
                headers={
                    "Accept": "application/json",
                    "User-Agent": "servicesite-xmr-quote/1",
                    "x-cg-demo-api-key": self.config.api_key,
                },
                timeout=float(self.config.timeout_seconds),
            )
        except requests.RequestException as exc:
            raise XmrRateUnavailableError("approved XMR pricing is unavailable") from exc

        if response.status_code != 200:
            raise XmrRateUnavailableError("approved XMR pricing is unavailable")
        if not isinstance(response.content, bytes) or not (
            0 < len(response.content) <= MAX_RESPONSE_BYTES
        ):
            raise XmrRateUnavailableError("approved XMR pricing response is invalid")

        try:
            payload = json.loads(
                response.content,
                parse_float=Decimal,
                parse_int=int,
                parse_constant=_reject_json_constant,
            )
            monero = payload["monero"]
            usd_per_xmr = monero["usd"]
            updated_timestamp = monero["last_updated_at"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise XmrRateUnavailableError("approved XMR pricing response is invalid") from exc

        if isinstance(usd_per_xmr, int) and not isinstance(usd_per_xmr, bool):
            usd_per_xmr = Decimal(usd_per_xmr)
        if not isinstance(usd_per_xmr, Decimal):
            raise XmrRateUnavailableError("approved XMR pricing value is invalid")
        if not usd_per_xmr.is_finite() or usd_per_xmr <= 0:
            raise XmrRateUnavailableError("approved XMR pricing value is invalid")
        if (
            isinstance(updated_timestamp, bool)
            or not isinstance(updated_timestamp, int)
            or updated_timestamp <= 0
        ):
            raise XmrRateUnavailableError("approved XMR pricing timestamp is invalid")

        try:
            provider_time = datetime.fromtimestamp(updated_timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise XmrRateUnavailableError("approved XMR pricing timestamp is invalid") from exc

        if provider_time > now + timedelta(minutes=1):
            raise XmrRateUnavailableError("approved XMR pricing timestamp is invalid")
        quote_time = min(provider_time, now)
        if now - quote_time > self.config.maximum_age:
            raise XmrRateStaleError("approved XMR pricing quote is stale")

        return XmrQuote(
            usd_per_xmr=usd_per_xmr,
            source=COINGECKO_RATE_SOURCE,
            quoted_at=quote_time,
        )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _require_aware_datetime(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise XmrRateUnavailableError("rate clock must be timezone-aware")
