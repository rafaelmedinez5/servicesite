from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from flask import current_app, session


_LOGIN_SESSION_KEY = "_login_captcha"
_SITE_SESSION_KEY = "_site_captcha"
_SITE_ACCESS_KEY = "_site_access_verified"
_MAX_AGE_SECONDS = 10 * 60


def issue_login_captcha() -> str:
    """Issue a short, single-use arithmetic challenge for the login form."""
    return _issue_captcha(_LOGIN_SESSION_KEY, "login-captcha")


def issue_site_captcha() -> str:
    """Issue a short, single-use arithmetic challenge for site entry."""
    return _issue_captcha(_SITE_SESSION_KEY, "site-captcha")


def _issue_captcha(session_key: str, purpose: str) -> str:
    left = 2 + secrets.randbelow(8)
    right = 2 + secrets.randbelow(8)
    challenge_id = secrets.token_urlsafe(18)
    answer = str(left + right)
    session[session_key] = {
        "id": challenge_id,
        "digest": _answer_digest(purpose, challenge_id, answer),
        "issued_at": int(time.time()),
    }
    return f"{left} + {right}"


def verify_login_captcha(candidate: str | None) -> bool:
    """Consume and verify the current login challenge."""
    return _verify_captcha(_LOGIN_SESSION_KEY, "login-captcha", candidate)


def verify_site_captcha(candidate: str | None) -> bool:
    """Consume and verify the current site-entry challenge."""
    return _verify_captcha(_SITE_SESSION_KEY, "site-captcha", candidate)


def site_access_verified() -> bool:
    return session.get(_SITE_ACCESS_KEY) is True


def mark_site_access_verified() -> None:
    session[_SITE_ACCESS_KEY] = True


def clear_session_preserving_site_access() -> None:
    """Reset authentication state without making a verified browser repeat entry."""
    verified = site_access_verified()
    session.clear()
    if verified:
        mark_site_access_verified()


def _verify_captcha(session_key: str, purpose: str, candidate: str | None) -> bool:
    challenge = session.pop(session_key, None)
    if not isinstance(challenge, dict) or not isinstance(candidate, str):
        return False
    challenge_id = challenge.get("id")
    expected = challenge.get("digest")
    issued_at = challenge.get("issued_at")
    answer = candidate.strip()
    if (
        not isinstance(challenge_id, str)
        or not isinstance(expected, str)
        or not isinstance(issued_at, int)
        or int(time.time()) - issued_at > _MAX_AGE_SECONDS
        or issued_at > int(time.time()) + 30
        or not answer.isascii()
        or not answer.isdecimal()
        or len(answer) > 3
    ):
        return False
    return hmac.compare_digest(
        expected, _answer_digest(purpose, challenge_id, answer)
    )


def _answer_digest(purpose: str, challenge_id: str, answer: str) -> str:
    secret = current_app.config["SECRET_KEY"]
    if not isinstance(secret, (str, bytes)):
        raise RuntimeError("SECRET_KEY must be text or bytes")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    message = f"{purpose}:{challenge_id}:{answer}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()
