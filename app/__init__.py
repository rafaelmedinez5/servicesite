from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

from flask import Flask, Request, abort, request

from app.academy_routes import register_academy
from app.admin import _usd_input, admin
from app.checkout_details import MAX_CHECKOUT_BODY_BYTES
from app.config import Settings
from app.customer_auth import register_customer_auth
from app.deliveries import MAX_DELIVERY_BODY_BYTES
from app.internal import register_internal
from app.inquiries import register_inquiries
from app.payments.xmr_wallet_rpc import atomic_to_xmr_str
from app.persistence import SQLiteDatabase, ServicesiteRepository
from app.service_images import MAX_UPLOAD_BYTES, ServiceImageStore
from app.shopping import register_shopping
from app.web import register_web


class _InMemoryUploadRequest(Request):
    @property
    def max_content_length(self):
        if self.endpoint == "shopping.checkout_cart":
            return MAX_CHECKOUT_BODY_BYTES
        if self.endpoint == "admin.purchase_fulfill":
            return MAX_DELIVERY_BODY_BYTES
        return super().max_content_length

    def _get_file_stream(
        self,
        total_content_length,
        content_type,
        filename=None,
        content_length=None,
    ):
        return io.BytesIO()


def create_app(test_config: dict | None = None) -> Flask:
    settings = Settings.from_env()

    app = Flask(__name__)
    app.request_class = _InMemoryUploadRequest
    app.config.from_mapping(
        ENVIRONMENT=settings.environment,
        SECRET_KEY=settings.secret_key,
        APP_HOST=settings.app_host,
        APP_PORT=settings.app_port,
        DB_PATH=settings.database_path,
        ADMIN_USERNAME=settings.admin_username,
        ADMIN_PATH=settings.admin_path,
        ADMIN_RECOVERY_PIN=settings.admin_recovery_pin,
        ADMIN_SESSION_HOURS=settings.admin_session_hours,
        PUBLIC_CONTACT_METHOD=settings.public_contact_method,
        PUBLIC_CONTACT_ADDRESS=settings.public_contact_address,
        PUBLIC_ONION_ADDRESS=settings.public_onion_address,
        XMR_WALLET_RPC_URL=settings.xmr_wallet_rpc_url,
        XMR_WALLET_RPC_USER=settings.xmr_wallet_rpc_user,
        XMR_WALLET_RPC_PASS=settings.xmr_wallet_rpc_password,
        XMR_ACCOUNT_INDEX=settings.xmr_account_index,
        XMR_RPC_TIMEOUT=settings.xmr_rpc_timeout_seconds,
        XMR_RPC_RETRIES=settings.xmr_rpc_max_attempts,
        XMR_RPC_RETRY_BACKOFF=settings.xmr_rpc_retry_backoff_seconds,
        XMR_MIN_CONFIRMATIONS=settings.xmr_min_confirmations,
        XMR_INVOICE_TTL_SECONDS=settings.xmr_invoice_ttl_seconds,
        XMR_RATE_SOURCE=settings.xmr_rate_source,
        COINGECKO_API_KEY=settings.coingecko_api_key,
        XMR_RATE_TIMEOUT=settings.xmr_rate_timeout_seconds,
        XMR_QUOTE_MAX_AGE_SECONDS=settings.xmr_quote_max_age_seconds,
        XMR_SWEEP_ENABLED=settings.xmr_sweep_enabled,
        XMR_COLD_ADDRESS=settings.xmr_cold_address,
        XMR_SWEEP_ACCOUNT_INDEX=settings.xmr_sweep_account_index,
        XMR_SWEEP_PRIORITY=settings.xmr_sweep_priority,
        XMR_SWEEP_RELAY=settings.xmr_sweep_relay,
        XMR_SWEEP_RECONCILE_SECONDS=settings.xmr_sweep_reconcile_seconds,
        X_INTERNAL_TOKEN=settings.internal_token,
        ALLOW_PUBLIC_XMR_WALLET_RPC=settings.allow_public_xmr_wallet_rpc,
    )
    if test_config:
        app.config.update(test_config)

    if app.config["ENVIRONMENT"] == "production" and app.config["ADMIN_PATH"] == "/admin":
        raise RuntimeError("Production ADMIN_PATH must not use the default /admin route")

    app.config["SERVICE_IMAGE_DIR"] = app.config.get("SERVICE_IMAGE_DIR") or str(
        Path(app.config["DB_PATH"]).parent / "service-images"
    )

    app.config.from_mapping(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + (64 * 1024),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=app.config["ENVIRONMENT"] == "production",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=app.config["ADMIN_SESSION_HOURS"]),
        SESSION_REFRESH_EACH_REQUEST=False,
    )

    @app.before_request
    def enforce_request_size():
        content_length = request.content_length
        if content_length is None:
            return None
        limit = (
            MAX_UPLOAD_BYTES + (64 * 1024)
            if request.endpoint == "admin.service_image_upload"
            else 16 * 1024
        )
        if request.endpoint == "shopping.checkout_cart":
            limit = MAX_CHECKOUT_BODY_BYTES
        if request.endpoint == "admin.purchase_fulfill":
            limit = MAX_DELIVERY_BODY_BYTES
        if content_length > limit:
            abort(413)
        return None

    app.extensions["servicesite_repository"] = app.config.get(
        "SERVICESITE_REPOSITORY",
        ServicesiteRepository(SQLiteDatabase(app.config["DB_PATH"])),
    )
    app.extensions["servicesite_image_store"] = app.config.get(
        "SERVICESITE_IMAGE_STORE",
        ServiceImageStore(app.config["SERVICE_IMAGE_DIR"]),
    )
    if app.config.get("SERVICESITE_RATE_PROVIDER") is not None:
        app.extensions["servicesite_rate_provider"] = app.config["SERVICESITE_RATE_PROVIDER"]
    if app.config.get("SERVICESITE_WALLET_CLIENT") is not None:
        app.extensions["servicesite_wallet_client"] = app.config["SERVICESITE_WALLET_CLIENT"]
    if app.config.get("SERVICESITE_NOW_FACTORY") is not None:
        app.extensions["servicesite_now_factory"] = app.config["SERVICESITE_NOW_FACTORY"]
    if app.config.get("SERVICESITE_RECONCILIATION_SERVICE") is not None:
        app.extensions["servicesite_reconciliation_service"] = app.config[
            "SERVICESITE_RECONCILIATION_SERVICE"
        ]

    register_inquiries(app)
    register_web(app)
    register_customer_auth(app)
    register_academy(app)
    register_shopping(app)
    app.register_blueprint(admin, url_prefix=app.config["ADMIN_PATH"])
    app.jinja_env.filters["atomic_xmr"] = atomic_to_xmr_str
    app.jinja_env.filters["usd_input"] = _usd_input
    register_internal(app)

    @app.get("/health")
    def health():
        return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}

    return app
