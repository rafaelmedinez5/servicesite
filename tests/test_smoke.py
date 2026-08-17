import pytest

from app import create_app
from app.config import Settings


def test_application_smoke(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "5100")

    app = create_app({"TESTING": True})
    client = app.test_client()

    assert client.get("/").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_data(as_text=True) == "OK"


def test_default_bind_is_servicesite_loopback_port(monkeypatch):
    monkeypatch.delenv("APP_HOST", raising=False)
    monkeypatch.delenv("APP_PORT", raising=False)

    settings = Settings.from_env()

    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 5100


def test_non_loopback_bind_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_HOST", "0.0.0.0")

    with pytest.raises(RuntimeError, match="loopback-only"):
        Settings.from_env()


def test_production_rejects_placeholder_secret(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "replace_with_at_least_32_random_characters")

    with pytest.raises(RuntimeError, match="Production SECRET_KEY"):
        Settings.from_env()


def test_production_requires_explicit_secret(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="Production SECRET_KEY"):
        Settings.from_env()


def _set_valid_production_xmr_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "s" * 32)
    monkeypatch.setenv("XMR_WALLET_RPC_URL", "http://127.0.0.1:28088/json_rpc")
    monkeypatch.setenv("XMR_WALLET_RPC_USER", "rpc-test-user")
    monkeypatch.setenv("XMR_WALLET_RPC_PASS", "p" * 16)
    monkeypatch.setenv("X_INTERNAL_TOKEN", "t" * 32)
    monkeypatch.setenv("XMR_SWEEP_ENABLED", "false")
    monkeypatch.setenv("ALLOW_PUBLIC_XMR_WALLET_RPC", "false")


def test_production_rejects_public_wallet_rpc_without_override(monkeypatch):
    _set_valid_production_xmr_environment(monkeypatch)
    monkeypatch.setenv("XMR_WALLET_RPC_URL", "https://rpc.example/json_rpc")

    with pytest.raises(RuntimeError, match="loopback/private"):
        Settings.from_env()


def test_production_public_wallet_rpc_requires_explicit_dangerous_override(monkeypatch):
    _set_valid_production_xmr_environment(monkeypatch)
    monkeypatch.setenv("XMR_WALLET_RPC_URL", "https://rpc.example/json_rpc")
    monkeypatch.setenv("ALLOW_PUBLIC_XMR_WALLET_RPC", "true")

    settings = Settings.from_env()

    assert settings.allow_public_xmr_wallet_rpc is True


def test_production_requires_digest_credentials(monkeypatch):
    _set_valid_production_xmr_environment(monkeypatch)
    monkeypatch.delenv("XMR_WALLET_RPC_PASS", raising=False)

    with pytest.raises(RuntimeError, match="XMR_WALLET_RPC_PASS"):
        Settings.from_env()


def test_production_sweep_requires_explicit_cold_destination(monkeypatch):
    _set_valid_production_xmr_environment(monkeypatch)
    monkeypatch.setenv("XMR_SWEEP_ENABLED", "true")
    monkeypatch.delenv("XMR_COLD_ADDRESS", raising=False)

    with pytest.raises(RuntimeError, match="XMR_COLD_ADDRESS"):
        Settings.from_env()


def test_xmr_configuration_defaults_are_safe(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.delenv("XMR_WALLET_RPC_URL", raising=False)
    monkeypatch.delenv("XMR_MIN_CONFIRMATIONS", raising=False)
    monkeypatch.delenv("XMR_SWEEP_ENABLED", raising=False)
    monkeypatch.delenv("ALLOW_PUBLIC_XMR_WALLET_RPC", raising=False)

    settings = Settings.from_env()

    assert settings.xmr_wallet_rpc_url == "http://127.0.0.1:28088/json_rpc"
    assert settings.xmr_min_confirmations == 10
    assert settings.xmr_sweep_enabled is False
    assert settings.allow_public_xmr_wallet_rpc is False


def test_settings_repr_hides_secrets(monkeypatch):
    _set_valid_production_xmr_environment(monkeypatch)
    settings = Settings.from_env()

    rendered = repr(settings)
    assert "s" * 32 not in rendered
    assert "p" * 16 not in rendered
    assert "t" * 32 not in rendered
    assert "rpc-test-user" not in rendered
