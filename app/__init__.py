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
