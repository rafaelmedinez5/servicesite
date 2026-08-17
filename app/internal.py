from __future__ import annotations

import ipaddress
import secrets
from datetime import timedelta

from flask import Blueprint, current_app, g, jsonify, request

from app.payments.xmr_reconciliation import (
    ReconciliationConfig,
    XmrReconciliationService,
    XmrReconciliationUnavailable,
)
from app.payments.xmr_wallet_rpc import WalletRpcConfig, XmrWalletRpcClient


internal = Blueprint("internal", __name__)


@internal.post("/internal/poll-xmr")
def poll_xmr():
    g.no_store = True
    g.private_response = True
    if not _is_loopback_request() or not _valid_internal_token():
        return jsonify({"ok": False, "error": "access_denied"}), 403

    try:
        summary = _reconciliation_service().poll()
    except XmrReconciliationUnavailable:
        return jsonify({"ok": False, "error": "wallet_unavailable"}), 503
    status_code = 200 if summary.errors == 0 else 503
    return jsonify(summary.as_dict()), status_code


def register_internal(app) -> None:
    app.register_blueprint(internal)


def _is_loopback_request() -> bool:
    remote_address = request.remote_addr
    if not isinstance(remote_address, str):
        return False
    try:
        return ipaddress.ip_address(remote_address).is_loopback
    except ValueError:
        return False


def _valid_internal_token() -> bool:
    expected = current_app.config.get("X_INTERNAL_TOKEN", "")
    candidate = request.headers.get("X-Internal-Token", "")
    if not isinstance(expected, str) or not expected:
        return False
    return secrets.compare_digest(expected, candidate)


def _reconciliation_service() -> XmrReconciliationService:
    injected = current_app.extensions.get("servicesite_reconciliation_service")
    if injected is not None:
        return injected

    wallet = current_app.extensions.get("servicesite_wallet_client")
    if wallet is None:
        wallet = XmrWalletRpcClient(
            WalletRpcConfig(
                url=current_app.config["XMR_WALLET_RPC_URL"],
                username=current_app.config["XMR_WALLET_RPC_USER"],
                password=current_app.config["XMR_WALLET_RPC_PASS"],
                account_index=current_app.config["XMR_ACCOUNT_INDEX"],
                timeout_seconds=current_app.config["XMR_RPC_TIMEOUT"],
                max_attempts=current_app.config["XMR_RPC_RETRIES"],
                retry_backoff_seconds=current_app.config["XMR_RPC_RETRY_BACKOFF"],
            )
        )

    attempts = current_app.config["XMR_RPC_RETRIES"]
    timeout = current_app.config["XMR_RPC_TIMEOUT"]
    backoff = current_app.config["XMR_RPC_RETRY_BACKOFF"]
    claim_seconds = (
        timeout * (attempts + 1)
        + backoff * sum(range(1, attempts))
        + 60
    )
    now_factory = current_app.extensions.get("servicesite_now_factory")
    kwargs = {"logger": current_app.logger}
    if now_factory is not None:
        kwargs["now_factory"] = now_factory
    return XmrReconciliationService(
        current_app.extensions["servicesite_repository"],
        wallet,
        ReconciliationConfig(
            account_index=current_app.config["XMR_ACCOUNT_INDEX"],
            sweep_enabled=current_app.config["XMR_SWEEP_ENABLED"],
            cold_address=current_app.config["XMR_COLD_ADDRESS"],
            sweep_account_index=current_app.config["XMR_SWEEP_ACCOUNT_INDEX"],
            sweep_priority=current_app.config["XMR_SWEEP_PRIORITY"],
            sweep_relay=current_app.config["XMR_SWEEP_RELAY"],
            claim_lease=timedelta(seconds=claim_seconds),
            uncertain_reconcile_delay=timedelta(
                seconds=current_app.config["XMR_SWEEP_RECONCILE_SECONDS"]
            ),
        ),
        **kwargs,
    )
