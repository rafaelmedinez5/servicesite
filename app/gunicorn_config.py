"""Gunicorn settings loaded only after the application environment is validated."""

from app.config import Settings


_settings = Settings.from_env()

bind = f"{_settings.app_host}:{_settings.app_port}"
workers = 2
threads = 1
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = None
errorlog = "-"
loglevel = "info"
capture_output = True
forwarded_allow_ips = ""
