from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

from app.preflight import (
    EXPECTED_UNITS,
    REQUIRED_WALLET_OPTIONS,
    Identity,
    PreflightPaths,
    render_report,
    run_preflight,
)


APPLICATION_SECRET = "application-secret-used-only-by-preflight-tests"
INTERNAL_SECRET = "internal-secret-used-only-by-preflight-tests"
RPC_PASSWORD = "rpc-password-used-only-by-preflight-tests"
RATE_SECRET = "rate-secret-used-only-by-preflight-tests"
WALLET_CONFIG_SECRET = "wallet-config-secret-used-only-by-preflight-tests"


def _write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def _make_paths(tmp_path: Path) -> PreflightPaths:
    repository_root = tmp_path / "repository"
    service_root = tmp_path / "service"
    application_root = service_root / "app"
    virtualenv = service_root / ".venv" / "bin"
    wallet_root = tmp_path / "wallet"
    etc_root = tmp_path / "etc"

    for path, mode in (
        (service_root, 0o750),
        (application_root, 0o750),
        (service_root / "instance", 0o700),
        (service_root / "logs", 0o750),
        (service_root / "secrets", 0o700),
        (wallet_root, 0o750),
        (wallet_root / "bin", 0o750),
        (wallet_root / "logs", 0o750),
        (wallet_root / "wallet", 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)

    _write(virtualenv / "python", "test executable\n", 0o750)
    _write(virtualenv / "gunicorn", "test executable\n", 0o750)
    _write(wallet_root / "bin" / "monero-wallet-rpc", "test executable\n", 0o750)
    _write(
        etc_root / "servicesite.env",
        "\n".join(
            (
                "ENVIRONMENT=production",
                f"SECRET_KEY={APPLICATION_SECRET}",
                "APP_HOST=127.0.0.1",
                "APP_PORT=5100",
                "DB_PATH=/opt/servicesite/instance/servicesite.db",
                f"X_INTERNAL_TOKEN={INTERNAL_SECRET}",
                "XMR_WALLET_RPC_URL=http://127.0.0.1:28088/json_rpc",
                "XMR_WALLET_RPC_USER=local-rpc-user",
                f"XMR_WALLET_RPC_PASS={RPC_PASSWORD}",
                "XMR_ACCOUNT_INDEX=0",
                "XMR_MIN_CONFIRMATIONS=10",
                "XMR_INVOICE_TTL_SECONDS=7200",
                "XMR_SWEEP_ENABLED=false",
                "ALLOW_PUBLIC_XMR_WALLET_RPC=false",
                "XMR_RATE_SOURCE=coingecko",
                f"COINGECKO_API_KEY={RATE_SECRET}",
            )
        )
        + "\n",
        0o600,
    )
    _write(etc_root / "wallet-rpc.conf", f"rpc-login={WALLET_CONFIG_SECRET}\n", 0o600)
    _write(
        etc_root / "torrc",
        "HiddenServiceDir /var/lib/tor/servicesite/\n"
        "HiddenServicePort 80 127.0.0.1:5100\n",
        0o644,
    )

    for unit in EXPECTED_UNITS:
        _write(repository_root / "deploy" / "systemd" / unit, f"source {unit}\n", 0o644)

    return PreflightPaths(
        repository_root=repository_root,
        service_root=service_root,
        application_root=application_root,
        virtualenv_python=virtualenv / "python",
        virtualenv_gunicorn=virtualenv / "gunicorn",
        instance_directory=service_root / "instance",
        application_log_directory=service_root / "logs",
        application_secret_directory=service_root / "secrets",
        environment_file=etc_root / "servicesite.env",
        wallet_root=wallet_root,
        wallet_bin_directory=wallet_root / "bin",
        wallet_log_directory=wallet_root / "logs",
        wallet_directory=wallet_root / "wallet",
        wallet_binary=wallet_root / "bin" / "monero-wallet-rpc",
        wallet_config=etc_root / "wallet-rpc.conf",
        tor_config=etc_root / "torrc",
        installed_unit_directory=tmp_path / "installed-units",
    )


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        stdout = ""
        returncode = 0
        if command[-1] == "--version":
            stdout = "Monero test build (v0.18.5.1-release)\n"
        elif command[-1] == "--help":
            stdout = "\n".join(REQUIRED_WALLET_OPTIONS)
        elif command[:3] == ["systemctl", "show", "--property=LoadState"]:
            stdout = "not-found\n"
        return subprocess.CompletedProcess(command, returncode, stdout, "")


def _identity(_name: str) -> Identity:
    return Identity(uid=os.getuid(), gid=os.getgid())


def test_install_preflight_passes_without_printing_secret_values(tmp_path):
    paths = _make_paths(tmp_path)
    runner = FakeRunner()

    checks = run_preflight(
        paths,
        phase="install",
        identity_resolver=_identity,
        runner=runner,
        port_probe=lambda _host, _port: False,
    )
    output = io.StringIO()

    assert render_report(checks, output)
    rendered = output.getvalue()
    assert "preflight passed" in rendered
    for secret in (
        APPLICATION_SECRET,
        INTERNAL_SECRET,
        RPC_PASSWORD,
        RATE_SECRET,
        WALLET_CONFIG_SECRET,
    ):
        assert secret not in rendered


def test_runtime_preflight_checks_only_harmless_health_operations(tmp_path):
    paths = _make_paths(tmp_path)
    paths.installed_unit_directory.mkdir()
    for unit in EXPECTED_UNITS:
        source = paths.source_unit_directory / unit
        installed = paths.installed_unit_directory / unit
        installed.write_bytes(source.read_bytes())
        installed.chmod(0o644)
    runner = FakeRunner()
    observed = {"web": 0, "wallet": 0}

    checks = run_preflight(
        paths,
        phase="runtime",
        identity_resolver=_identity,
        runner=runner,
        port_probe=lambda _host, _port: True,
        web_probe=lambda: observed.__setitem__("web", observed["web"] + 1) or True,
        wallet_probe=lambda _settings: observed.__setitem__(
            "wallet", observed["wallet"] + 1
        )
        or True,
    )

    assert all(check.passed for check in checks)
    assert observed == {"web": 1, "wallet": 1}
    assert any(command[:2] == ["systemctl", "is-active"] for command in runner.commands)
    flattened = "\n".join(" ".join(command) for command in runner.commands)
    for forbidden in (" start ", " stop ", " restart ", " enable ", " disable ", "daemon-reload"):
        assert forbidden not in f" {flattened} "


def test_preflight_rejects_private_file_with_group_access(tmp_path):
    paths = _make_paths(tmp_path)
    paths.environment_file.chmod(0o640)

    checks = run_preflight(
        paths,
        phase="install",
        identity_resolver=_identity,
        runner=FakeRunner(),
        port_probe=lambda _host, _port: False,
    )

    result = next(check for check in checks if check.name == "application environment file")
    assert not result.passed
    assert APPLICATION_SECRET not in result.detail


def test_preflight_code_contains_no_payment_or_service_mutation_commands():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "preflight.py").read_text()
    wrapper = (root / "scripts" / "preflight").read_text()

    assert "get_height" in source
    for forbidden in (
        "create_address(",
        "create_subaddress(",
        "sweep_all(",
        "transfer(",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "systemctl disable",
        "daemon-reload",
    ):
        assert forbidden not in source
        assert forbidden not in wrapper
