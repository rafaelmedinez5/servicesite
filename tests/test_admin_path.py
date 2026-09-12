from __future__ import annotations

import pytest

from app import create_app
from app.config import Settings
from app.persistence import SQLiteDatabase, ServicesiteRepository


def _app_with_path(tmp_path, monkeypatch, admin_path: str):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-admin-path-session-secret")
    monkeypatch.setenv("ADMIN_PATH", admin_path)
    database = SQLiteDatabase(tmp_path / "admin-path.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    return create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "admin-path.db"),
            "SERVICESITE_REPOSITORY": repository,
        }
    )


def test_custom_admin_path_replaces_default_route(tmp_path, monkeypatch):
    app = _app_with_path(tmp_path, monkeypatch, "/ops-7f3b8d91")
    client = app.test_client()

    configured = client.get("/ops-7f3b8d91/login")
    old_default = client.get("/admin/login")

    assert configured.status_code == 200
    assert old_default.status_code == 404
    assert app.config["ADMIN_PATH"] == "/ops-7f3b8d91"


@pytest.mark.parametrize(
    "value",
    [
        "admin",
        "/Admin",
        "/two/segments",
        "/under_score",
        "/a",
        "/contact",
    ],
)
def test_admin_path_rejects_invalid_or_conflicting_values(monkeypatch, value):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ADMIN_PATH", value)

    with pytest.raises(RuntimeError, match="ADMIN_PATH"):
        Settings.from_env()


def test_development_default_remains_admin_for_compatibility(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("ADMIN_PATH", raising=False)

    assert Settings.from_env().admin_path == "/admin"


def test_production_refuses_default_admin_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-admin-path-session-secret")
    monkeypatch.delenv("ADMIN_PATH", raising=False)
    database = SQLiteDatabase(tmp_path / "production-admin-path.db")
    database.initialize()
    repository = ServicesiteRepository(database)

    with pytest.raises(RuntimeError, match="Production ADMIN_PATH"):
        create_app(
            {
                "TESTING": True,
                "ENVIRONMENT": "production",
                "DB_PATH": str(tmp_path / "production-admin-path.db"),
                "SERVICESITE_REPOSITORY": repository,
            }
        )
