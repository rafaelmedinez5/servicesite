from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from app.config import LOOPBACK_HOSTS, Settings


MAX_RESPONSE_BYTES = 16 * 1024
POLL_HTTP_TIMEOUT_SECONDS = 270


class PollClientError(RuntimeError):
    """A sanitized local polling failure suitable for an operator journal."""


class Response(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "Response": ...

    def __exit__(self, *args: object) -> None: ...


class PollOpener(Protocol):
    def open(self, request: Request, timeout: int) -> Response: ...


@dataclass(frozen=True)
class PollResult:
    open_invoices: int
    processed: int
    skipped_locked: int
    settled: int
    errors: int


def poll_once(
    settings: Settings,
    *,
    opener: PollOpener | None = None,
    timeout_seconds: int = POLL_HTTP_TIMEOUT_SECONDS,
) -> PollResult:
    """Call the loopback-only mutation endpoint without placing its token in argv."""

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        raise PollClientError("poll timeout is invalid")
    if not 1 <= timeout_seconds <= POLL_HTTP_TIMEOUT_SECONDS:
        raise PollClientError("poll timeout is outside the safe range")

    host = settings.app_host
    if host not in LOOPBACK_HOSTS:
        raise PollClientError("poll endpoint must remain loopback-only")
    if host == "::1":
        host = "[::1]"
    url = f"http://{host}:{settings.app_port}/internal/poll-xmr"
    request = Request(
        url,
        method="POST",
        headers={
            "Accept": "application/json",
            "X-Internal-Token": settings.internal_token,
        },
    )
    local_opener = opener or build_opener(ProxyHandler({}))
    try:
        with local_opener.open(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise PollClientError("poll endpoint returned a failure")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise PollClientError("poll endpoint is unavailable") from exc

    if len(payload) > MAX_RESPONSE_BYTES:
        raise PollClientError("poll response exceeded the safe size")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PollClientError("poll response is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise PollClientError("poll response reported a failure")

    values = {}
    for key in ("open_invoices", "processed", "skipped_locked", "settled", "errors"):
        value = decoded.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PollClientError("poll response counters are invalid")
        values[key] = value
    if values["errors"] != 0:
        raise PollClientError("poll response contains reconciliation errors")
    return PollResult(**values)


def main() -> int:
    try:
        result = poll_once(Settings.from_env())
    except (PollClientError, RuntimeError):
        print("xmr_poll result=failed", file=sys.stderr)
        return 1

    print(
        "xmr_poll result=ok "
        f"open_invoices={result.open_invoices} "
        f"processed={result.processed} "
        f"skipped_locked={result.skipped_locked} "
        f"settled={result.settled} errors=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
