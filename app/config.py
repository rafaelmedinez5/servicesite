from __future__ import annotations

import os
from dataclasses import dataclass


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PLACEHOLDER_SECRET_VALUES = {
    "",
    "change-me",
    "changeme",
    "development-only-not-for-production",
    "replace_me",
    "replace_with_at_least_32_random_characters",
}


def _parse_port(raw_value: str) -> int:
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("APP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("APP_PORT must be between 1 and 65535")
    return port


@dataclass(frozen=True)
class Settings:
    environment: str
    secret_key: str
    app_host: str
    app_port: int
    database_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        environment = os.getenv("ENVIRONMENT", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise RuntimeError("ENVIRONMENT must be development, test, or production")

        secret_key = os.getenv("SECRET_KEY", "development-only-not-for-production").strip()
        app_host = os.getenv("APP_HOST", "127.0.0.1").strip()
        if app_host not in LOOPBACK_HOSTS:
            raise RuntimeError("APP_HOST must remain loopback-only")

        if environment == "production":
            if secret_key.lower() in PLACEHOLDER_SECRET_VALUES or len(secret_key) < 32:
                raise RuntimeError("Production SECRET_KEY must be random and at least 32 characters")

        database_path = os.getenv(
            "DB_PATH", "/opt/servicesite/instance/servicesite.db"
        ).strip()
        if not database_path:
            raise RuntimeError("DB_PATH is required")

        return cls(
            environment=environment,
            secret_key=secret_key,
            app_host=app_host,
            app_port=_parse_port(os.getenv("APP_PORT", "5100").strip()),
            database_path=database_path,
        )
