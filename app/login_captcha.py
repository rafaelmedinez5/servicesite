from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from flask import current_app, session


_SESSION_KEY = "_login_captcha"
_MAX_AGE_SECONDS = 10 * 60


def issue_login_captcha() -> str:
    """Issue a short, single-use arithmetic challenge for the login form."""
    left = 2 + secrets.randbelow(8)
    right = 2 + secrets.randbelow(8)
    challenge_id = secrets.token_urlsafe(18)
    answer = str(left + right)
    session[_SESSION_KEY] = {
        "id": challenge_id,
        "digest": _answer_digest(challenge_id, answer),
        "issued_at": int(time.time()),
    }
    return f"{left} + {right}"


def verify_login_captcha(candidate: str | None) -> bool:
    """Consume and verify the current login challenge."""
    challenge = session.pop(_SESSION_KEY, None)
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
    return hmac.compare_digest(expected, _answer_digest(challenge_id, answer))


def _answer_digest(challenge_id: str, answer: str) -> str:
    secret = current_app.config["SECRET_KEY"]
    if not isinstance(secret, (str, bytes)):
        raise RuntimeError("SECRET_KEY must be text or bytes")
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    message = f"login-captcha:{challenge_id}:{answer}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()
