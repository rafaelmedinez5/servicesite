from __future__ import annotations

import argparse
import grp
import os
import pwd
import socket
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence, TextIO
from urllib.request import ProxyHandler, build_opener

from dotenv import dotenv_values

from app.config import Settings
from app.payments.xmr_wallet_rpc import WalletRpcConfig, XmrWalletRpcClient


EXPECTED_MONERO_VERSION = "v0.18.5.1"
EXPECTED_UNITS = (
    "servicesite-wallet-rpc.service",
    "servicesite-web.service",
    "servicesite-poll-xmr.service",
    "servicesite-poll-xmr.timer",
)
REQUIRED_WALLET_OPTIONS = (
    "--config-file",
    "--daemon-address",
    "--log-file",
    "--password-file",
    "--proxy",
    "--rpc-bind-ip",
    "--rpc-bind-port",
    "--rpc-login",
    "--stagenet",
    "--untrusted-daemon",
    "--wallet-file",
)


@dataclass(frozen=True)
class PreflightPaths:
    repository_root: Path
    service_root: Path = Path("/opt/servicesite")
    application_root: Path = Path("/opt/servicesite/app")
    virtualenv_python: Path = Path("/opt/servicesite/.venv/bin/python")
    virtualenv_gunicorn: Path = Path("/opt/servicesite/.venv/bin/gunicorn")
    instance_directory: Path = Path("/opt/servicesite/instance")
    application_log_directory: Path = Path("/opt/servicesite/logs")
    application_secret_directory: Path = Path("/opt/servicesite/secrets")
    environment_file: Path = Path("/etc/servicesite/servicesite.env")
    wallet_root: Path = Path("/opt/monero")
    wallet_bin_directory: Path = Path("/opt/monero/bin")
    wallet_log_directory: Path = Path("/opt/monero/logs")
    wallet_directory: Path = Path("/opt/monero/wallet")
    wallet_binary: Path = Path("/opt/monero/bin/monero-wallet-rpc")
    wallet_config: Path = Path("/etc/servicesite/monero-wallet-rpc.conf")
    tor_config: Path = Path("/etc/tor/torrc")
    installed_unit_directory: Path = Path("/etc/systemd/system")

    @property
    def source_unit_directory(self) -> Path:
        return self.repository_root / "deploy" / "systemd"


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
IdentityResolver = Callable[[str], Identity]
PortProbe = Callable[[str, int], bool]
WebProbe = Callable[[], bool]
WalletProbe = Callable[[Settings], bool]


def _resolve_identity(name: str) -> Identity:
    user = pwd.getpwnam(name)
    group = grp.getgrnam(name)
    return Identity(uid=user.pw_uid, gid=group.gr_gid)


def _run_captured(
    command: Sequence[str],
    *,
    runner: CommandRunner = subprocess.run,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(command),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _web_is_healthy() -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:5100/health", timeout=3) as response:
            return response.status == 200 and response.read(3) == b"OK"
    except OSError:
        return False


def _wallet_is_healthy(settings: Settings) -> bool:
    try:
        client = XmrWalletRpcClient(
            WalletRpcConfig(
                url=settings.xmr_wallet_rpc_url,
                username=settings.xmr_wallet_rpc_user,
                password=settings.xmr_wallet_rpc_password,
                account_index=settings.xmr_account_index,
                timeout_seconds=min(settings.xmr_rpc_timeout_seconds, 5),
                max_attempts=1,
                retry_backoff_seconds=0,
            )
        )
        client.get_height()
        if settings.xmr_sweep_enabled:
            validation = client.validate_address(settings.xmr_cold_address)
            if (
                not validation.valid
                or validation.integrated
                or validation.nettype != "mainnet"
            ):
                return False
        return True
    except Exception:
        return False


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _directory_check(
    name: str,
    path: Path,
    identity: Identity,
    *,
    allowed_modes: set[int],
) -> Check:
    try:
        metadata = path.lstat()
    except OSError:
        return Check(name, False, "required directory is missing or inaccessible")
    passed = (
        stat.S_ISDIR(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == identity.uid
        and metadata.st_gid == identity.gid
        and stat.S_IMODE(metadata.st_mode) in allowed_modes
    )
    return Check(
        name,
        passed,
        "directory type, ownership, and mode are restricted"
        if passed
        else "directory type, ownership, or mode is unsafe",
    )


def _private_file_check(name: str, path: Path, identity: Identity) -> Check:
    try:
        metadata = path.lstat()
    except OSError:
        return Check(name, False, "required private file is missing or inaccessible")
    passed = (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == identity.uid
        and metadata.st_gid == identity.gid
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )
    return Check(
        name,
        passed,
        "private file ownership and mode are restricted"
        if passed
        else "private file type, ownership, or mode is unsafe",
    )


def _executable_check(name: str, path: Path, identity: Identity) -> Check:
    try:
        metadata = path.lstat()
    except OSError:
        return Check(name, False, "required executable is missing or inaccessible")
    mode = stat.S_IMODE(metadata.st_mode)
    passed = (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and metadata.st_uid == identity.uid
        and metadata.st_gid == identity.gid
        and bool(mode & stat.S_IXUSR)
        and not bool(mode & 0o022)
    )
    return Check(
        name,
        passed,
        "executable type, ownership, and mode are restricted"
        if passed
        else "executable type, ownership, or mode is unsafe",
    )


@contextmanager
def _isolated_environment(values: Mapping[str, str]):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _load_validated_settings(path: Path) -> tuple[Check, Settings | None]:
    try:
        raw_values = dotenv_values(path, interpolate=False)
        if not raw_values or any(value is None for value in raw_values.values()):
            raise RuntimeError("invalid environment assignment")
        values = {str(key): str(value) for key, value in raw_values.items()}
        with _isolated_environment(values):
            settings = Settings.from_env()
        expected = (
            settings.environment == "production"
            and settings.app_host == "127.0.0.1"
            and settings.app_port == 5100
            and Path(settings.database_path) == Path("/opt/servicesite/instance/servicesite.db")
            and settings.xmr_wallet_rpc_url == "http://127.0.0.1:28088/json_rpc"
            and settings.xmr_min_confirmations == 10
            and settings.xmr_invoice_ttl_seconds == 7200
            and (
                not settings.xmr_sweep_enabled
                or settings.xmr_sweep_account_index == settings.xmr_account_index
            )
            and not settings.allow_public_xmr_wallet_rpc
        )
        if not expected:
            raise RuntimeError("deployment decision mismatch")
    except Exception:
        return (
            Check(
                "application environment",
                False,
                "external environment failed production validation; values were not displayed",
            ),
            None,
        )
    return (
        Check(
            "application environment",
            True,
            "production settings match the recorded safe deployment decisions",
        ),
        settings,
    )


def _wallet_binary_checks(
    path: Path,
    *,
    runner: CommandRunner,
) -> Iterable[Check]:
    try:
        version = _run_captured((str(path), "--version"), runner=runner)
        version_ok = version.returncode == 0 and EXPECTED_MONERO_VERSION in (
            version.stdout + version.stderr
        )
    except (OSError, subprocess.SubprocessError):
        version_ok = False
    yield Check(
        "Monero binary version",
        version_ok,
        f"verified expected {EXPECTED_MONERO_VERSION} output"
        if version_ok
        else f"binary did not report expected {EXPECTED_MONERO_VERSION}",
    )

    try:
        help_result = _run_captured((str(path), "--help"), runner=runner)
        help_text = help_result.stdout + help_result.stderr
        options_ok = help_result.returncode == 0 and all(
            option in help_text for option in REQUIRED_WALLET_OPTIONS
        )
    except (OSError, subprocess.SubprocessError):
        options_ok = False
    yield Check(
        "Monero binary options",
        options_ok,
        "required wallet-RPC options are supported"
        if options_ok
        else "one or more required wallet-RPC options are unavailable",
    )


def _tor_check(path: Path) -> Check:
    try:
        directives = []
        for raw_line in path.read_text(errors="replace").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line:
                directives.append(" ".join(line.split()))
    except OSError:
        return Check("Tor mapping", False, "Tor configuration is missing or inaccessible")

    required = (
        "HiddenServiceDir /var/lib/tor/servicesite/",
        "HiddenServicePort 80 127.0.0.1:5100",
    )
    passed = all(directives.count(item) == 1 for item in required)
    return Check(
        "Tor mapping",
        passed,
        "new hidden service maps only to the recorded loopback web listener"
        if passed
        else "required unique hidden-service directory and loopback mapping were not found",
    )


def _source_unit_checks(paths: PreflightPaths) -> Iterable[Check]:
    for unit in EXPECTED_UNITS:
        source = paths.source_unit_directory / unit
        try:
            metadata = source.lstat()
            passed = stat.S_ISREG(metadata.st_mode) and not source.is_symlink()
        except OSError:
            passed = False
        yield Check(
            f"source unit {unit}",
            passed,
            "reviewed source unit is present" if passed else "reviewed source unit is missing",
        )


def _installed_unit_checks(paths: PreflightPaths) -> Iterable[Check]:
    for unit in EXPECTED_UNITS:
        source = paths.source_unit_directory / unit
        installed = paths.installed_unit_directory / unit
        try:
            passed = (
                installed.is_file()
                and not installed.is_symlink()
                and _mode(installed) == 0o644
                and installed.read_bytes() == source.read_bytes()
            )
        except OSError:
            passed = False
        yield Check(
            f"installed unit {unit}",
            passed,
            "installed unit exactly matches reviewed source"
            if passed
            else "installed unit is missing, unsafe, or differs from reviewed source",
        )


def run_preflight(
    paths: PreflightPaths,
    *,
    phase: str,
    identity_resolver: IdentityResolver = _resolve_identity,
    runner: CommandRunner = subprocess.run,
    port_probe: PortProbe = _port_is_open,
    web_probe: WebProbe = _web_is_healthy,
    wallet_probe: WalletProbe = _wallet_is_healthy,
) -> list[Check]:
    if phase not in {"install", "runtime"}:
        raise ValueError("phase must be install or runtime")

    checks: list[Check] = []
    identities: dict[str, Identity] = {}
    for name in ("servicesite", "xmrwallet"):
        try:
            identities[name] = identity_resolver(name)
            checks.append(Check(f"identity {name}", True, "dedicated user and group exist"))
        except (KeyError, OSError):
            checks.append(Check(f"identity {name}", False, "dedicated user or group is missing"))

    try:
        root_identity = identity_resolver("root")
        checks.append(Check("identity root", True, "system root identity exists"))
    except (KeyError, OSError):
        root_identity = None
        checks.append(Check("identity root", False, "system root identity is missing"))

    try:
        package_audit = _run_captured(("dpkg", "--audit"), runner=runner)
        packages_ok = package_audit.returncode == 0 and not package_audit.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        packages_ok = False
    checks.append(
        Check(
            "package database",
            packages_ok,
            "package database audit is clean"
            if packages_ok
            else "package database audit requires operator repair",
        )
    )

    if "servicesite" in identities:
        app_identity = identities["servicesite"]
        for name, path, modes in (
            ("service root", paths.service_root, {0o750}),
            ("application root", paths.application_root, {0o750}),
            ("instance directory", paths.instance_directory, {0o700}),
            ("application log directory", paths.application_log_directory, {0o750}),
            ("application secret directory", paths.application_secret_directory, {0o700}),
        ):
            checks.append(_directory_check(name, path, app_identity, allowed_modes=modes))
        checks.append(
            _executable_check("virtualenv Python", paths.virtualenv_python, app_identity)
        )
        checks.append(
            _executable_check("virtualenv Gunicorn", paths.virtualenv_gunicorn, app_identity)
        )
        checks.append(
            _private_file_check("application environment file", paths.environment_file, app_identity)
        )
        try:
            writable = (
                _run_captured(
                    (
                        "runuser",
                        "-u",
                        "servicesite",
                        "--",
                        "test",
                        "-w",
                        str(paths.instance_directory),
                    ),
                    runner=runner,
                ).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            writable = False
        checks.append(
            Check(
                "database directory access",
                writable,
                "application identity can write the instance directory"
                if writable
                else "application identity cannot write the instance directory",
            )
        )

    if "xmrwallet" in identities:
        wallet_identity = identities["xmrwallet"]
        immutable_identity = Identity(
            uid=root_identity.uid if root_identity is not None else -1,
            gid=wallet_identity.gid,
        )
        checks.append(
            _directory_check(
                "wallet root", paths.wallet_root, immutable_identity, allowed_modes={0o750}
            )
        )
        checks.append(
            _directory_check(
                "wallet binary directory",
                paths.wallet_bin_directory,
                immutable_identity,
                allowed_modes={0o750},
            )
        )
        checks.append(
            _directory_check(
                "wallet log directory",
                paths.wallet_log_directory,
                wallet_identity,
                allowed_modes={0o750},
            )
        )
        checks.append(
            _directory_check(
                "wallet private directory",
                paths.wallet_directory,
                wallet_identity,
                allowed_modes={0o700},
            )
        )
        binary_check = _executable_check(
            "wallet-RPC executable", paths.wallet_binary, immutable_identity
        )
        checks.append(binary_check)
        checks.append(_private_file_check("wallet-RPC configuration", paths.wallet_config, wallet_identity))
        if binary_check.passed:
            checks.extend(_wallet_binary_checks(paths.wallet_binary, runner=runner))
        else:
            checks.extend(
                (
                    Check("Monero binary version", False, "wallet-RPC executable is unavailable"),
                    Check("Monero binary options", False, "wallet-RPC executable is unavailable"),
                )
            )

    settings_check, settings = _load_validated_settings(paths.environment_file)
    checks.append(settings_check)
    checks.append(_tor_check(paths.tor_config))
    checks.extend(_source_unit_checks(paths))

    port_states = {
        5100: port_probe("127.0.0.1", 5100),
        28088: port_probe("127.0.0.1", 28088),
    }
    for port, label in ((5100, "web port"), (28088, "wallet-RPC port")):
        expected_open = phase == "runtime"
        passed = port_states[port] is expected_open
        checks.append(
            Check(
                label,
                passed,
                "recorded loopback listener is reachable"
                if passed and expected_open
                else "recorded loopback port is available"
                if passed
                else "listener state does not match the selected preflight phase",
            )
        )

    if phase == "install":
        for unit in EXPECTED_UNITS:
            try:
                state = _run_captured(
                    ("systemctl", "show", "--property=LoadState", "--value", unit),
                    runner=runner,
                )
                absent = state.returncode == 0 and state.stdout.strip() == "not-found"
            except (OSError, subprocess.SubprocessError):
                absent = False
            checks.append(
                Check(
                    f"unit name {unit}",
                    absent,
                    "unit name is available" if absent else "unit name is already loaded or unverifiable",
                )
            )
    else:
        checks.extend(_installed_unit_checks(paths))
        for unit in ("servicesite-wallet-rpc.service", "servicesite-web.service"):
            try:
                active = (
                    _run_captured(("systemctl", "is-active", "--quiet", unit), runner=runner).returncode
                    == 0
                )
            except (OSError, subprocess.SubprocessError):
                active = False
            checks.append(
                Check(
                    f"active unit {unit}",
                    active,
                    "required runtime unit is active"
                    if active
                    else "required runtime unit is not active",
                )
            )
        web_healthy = web_probe()
        checks.append(
            Check(
                "web health",
                web_healthy,
                "loopback health endpoint returned the fixed success response"
                if web_healthy
                else "loopback health endpoint is unavailable or returned an unexpected response",
            )
        )
        wallet_healthy = settings is not None and wallet_probe(settings)
        checks.append(
            Check(
                "wallet-RPC health",
                wallet_healthy,
                "authenticated wallet and enabled sweep-destination checks succeeded"
                if wallet_healthy
                else "authenticated wallet check or sweep destination validation failed",
            )
        )

    return checks


def render_report(checks: Iterable[Check], stream: TextIO) -> bool:
    results = list(checks)
    for check in results:
        status = "PASS" if check.passed else "FAIL"
        stream.write(f"[{status}] {check.name}: {check.detail}\n")
    passed = all(check.passed for check in results)
    stream.write(
        "preflight passed; no files, services, invoices, or wallet state were modified\n"
        if passed
        else "preflight failed; correct the reported conditions before any operator action\n"
    )
    return passed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only servicesite deployment preflight")
    parser.add_argument(
        "--phase",
        choices=("install", "runtime"),
        default="install",
        help="install expects unused ports/unit names; runtime expects active loopback services",
    )
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    checks = run_preflight(PreflightPaths(repository_root=repository_root), phase=arguments.phase)
    return 0 if render_report(checks, sys.stdout) else 1


if __name__ == "__main__":
    raise SystemExit(main())
