from __future__ import annotations

from flask import Flask, render_template

from app.config import Settings


def create_app(test_config: dict | None = None) -> Flask:
    settings = Settings.from_env()

    app = Flask(__name__)
    app.config.from_mapping(
        ENVIRONMENT=settings.environment,
        SECRET_KEY=settings.secret_key,
        APP_HOST=settings.app_host,
        APP_PORT=settings.app_port,
        DB_PATH=settings.database_path,
        XMR_WALLET_RPC_URL=settings.xmr_wallet_rpc_url,
        XMR_WALLET_RPC_USER=settings.xmr_wallet_rpc_user,
        XMR_WALLET_RPC_PASS=settings.xmr_wallet_rpc_password,
        XMR_ACCOUNT_INDEX=settings.xmr_account_index,
        XMR_RPC_TIMEOUT=settings.xmr_rpc_timeout_seconds,
        XMR_RPC_RETRIES=settings.xmr_rpc_max_attempts,
        XMR_RPC_RETRY_BACKOFF=settings.xmr_rpc_retry_backoff_seconds,
        XMR_MIN_CONFIRMATIONS=settings.xmr_min_confirmations,
        XMR_INVOICE_TTL_SECONDS=settings.xmr_invoice_ttl_seconds,
        XMR_SWEEP_ENABLED=settings.xmr_sweep_enabled,
        XMR_COLD_ADDRESS=settings.xmr_cold_address,
        XMR_SWEEP_ACCOUNT_INDEX=settings.xmr_sweep_account_index,
        XMR_SWEEP_PRIORITY=settings.xmr_sweep_priority,
        XMR_SWEEP_RELAY=settings.xmr_sweep_relay,
        X_INTERNAL_TOKEN=settings.internal_token,
        ALLOW_PUBLIC_XMR_WALLET_RPC=settings.allow_public_xmr_wallet_rpc,
    )
    if test_config:
        app.config.update(test_config)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}

    return app
