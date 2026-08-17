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
