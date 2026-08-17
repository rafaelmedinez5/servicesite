from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from app.poll_client import PollClientError, poll_once


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = json.dumps(payload).encode()
        self.status = status

    def read(self, amount=-1):
        return self.payload[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _settings():
    return SimpleNamespace(
        app_host="127.0.0.1",
        app_port=5100,
        internal_token="test-only-internal-token-that-is-long",
    )


def test_poll_client_uses_validated_loopback_configuration_and_header():
    opener = FakeOpener(
        FakeResponse(
            {
                "ok": True,
                "open_invoices": 3,
                "processed": 2,
                "skipped_locked": 1,
                "settled": 1,
                "errors": 0,
            }
        )
    )

    result = poll_once(_settings(), opener=opener, timeout_seconds=30)
    request, timeout = opener.calls[0]

    assert request.full_url == "http://127.0.0.1:5100/internal/poll-xmr"
    assert dict(request.header_items())["X-internal-token"] == _settings().internal_token
    assert timeout == 30
    assert result.processed == 2
    assert result.settled == 1


def test_poll_client_refuses_an_injected_non_loopback_host():
    settings = _settings()
    settings.app_host = "203.0.113.10"
    opener = FakeOpener(FakeResponse({}))

    with pytest.raises(PollClientError, match="loopback-only"):
        poll_once(settings, opener=opener, timeout_seconds=30)

    assert opener.calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        URLError("test-only failure"),
        FakeResponse({"ok": False, "error": "wallet_unavailable"}),
        FakeResponse({"ok": True, "processed": "not-an-integer"}),
    ],
)
def test_poll_client_fails_with_sanitized_error(outcome):
    with pytest.raises(PollClientError) as raised:
        poll_once(_settings(), opener=FakeOpener(outcome), timeout_seconds=30)

    assert _settings().internal_token not in str(raised.value)
    assert "test-only failure" not in str(raised.value)


def test_units_are_separate_hardened_and_contain_no_secrets():
    units = {
        path.name: path.read_text()
        for path in SYSTEMD.iterdir()
        if path.suffix in {".service", ".timer"}
    }

    assert set(units) == {
        "servicesite-web.service",
        "servicesite-poll-xmr.service",
        "servicesite-poll-xmr.timer",
        "servicesite-wallet-rpc.service",
    }
    assert "User=servicesite" in units["servicesite-web.service"]
    assert "Group=servicesite" in units["servicesite-web.service"]
    assert "EnvironmentFile=/etc/servicesite/servicesite.env" in units[
        "servicesite-web.service"
    ]
    assert "WorkingDirectory=/opt/servicesite/app" in units[
        "servicesite-web.service"
    ]
    assert "User=xmrwallet" in units["servicesite-wallet-rpc.service"]
    assert "--config-file=/etc/servicesite/monero-wallet-rpc.conf" in units[
        "servicesite-wallet-rpc.service"
    ]
    assert "OnCalendar=*-*-* *:*:00" in units["servicesite-poll-xmr.timer"]
    assert "Persistent=true" in units["servicesite-poll-xmr.timer"]

    rendered = "\n".join(units.values())
    for forbidden in (
        "/opt/salessite",
        "127.0.0.1:5000",
        "X_INTERNAL_TOKEN=",
        "rpc-login=",
        "wallet-file=",
        "password-file=",
        "daemon-address=",
    ):
        assert forbidden not in rendered


def test_poll_wrapper_derives_paths_and_never_places_token_in_arguments():
    wrapper = (ROOT / "scripts" / "poll-xmr").read_text()

    assert "/opt/servicesite" not in wrapper
    assert "X_INTERNAL_TOKEN" not in wrapper
    assert "-m app.poll_client" in wrapper


def test_systemd_verifier_is_syntax_only_and_non_mutating():
    verifier = (ROOT / "scripts" / "verify-systemd").read_text()

    assert "ExecStart=/bin/true" in verifier
    assert "systemd-analyze verify" in verifier
    for forbidden in ("systemctl", "daemon-reload", "enable", "start", "stop"):
        assert forbidden not in verifier


def test_gunicorn_configuration_disables_bearer_url_access_logs(monkeypatch):
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "5100")

    namespace = {}
    exec((ROOT / "app" / "gunicorn_config.py").read_text(), namespace)

    assert namespace["bind"] == "127.0.0.1:5100"
    assert namespace["accesslog"] is None
    assert namespace["forwarded_allow_ips"] == ""
