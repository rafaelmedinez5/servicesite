from __future__ import annotations

import secrets

from flask import session


_CSRF_SESSION_KEY = "_csrf_token"
_CHECKOUT_NONCES_SESSION_KEY = "_checkout_nonces"
_MAX_ACTIVE_CHECKOUT_NONCES = 5


class FormSecurityError(ValueError):
    """A form token was missing, malformed, expired, or already consumed."""


def csrf_token() -> str:
    token = session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def require_csrf(candidate: str | None) -> None:
    expected = session.get(_CSRF_SESSION_KEY)
    if (
        not isinstance(expected, str)
        or not isinstance(candidate, str)
        or len(candidate) != len(expected)
        or not secrets.compare_digest(expected, candidate)
    ):
        raise FormSecurityError("invalid CSRF token")


def issue_checkout_nonce() -> str:
    nonce = secrets.token_urlsafe(24)
    active = session.get(_CHECKOUT_NONCES_SESSION_KEY, [])
    if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
        active = []
    active.append(nonce)
    session[_CHECKOUT_NONCES_SESSION_KEY] = active[-_MAX_ACTIVE_CHECKOUT_NONCES:]
    return nonce


def consume_checkout_nonce(candidate: str | None) -> None:
    active = session.get(_CHECKOUT_NONCES_SESSION_KEY, [])
    if (
        not isinstance(active, list)
        or not isinstance(candidate, str)
        or not 16 <= len(candidate) <= 128
    ):
        raise FormSecurityError("invalid checkout nonce")

    matching_index = next(
        (
            index
            for index, nonce in enumerate(active)
            if isinstance(nonce, str) and secrets.compare_digest(nonce, candidate)
        ),
        None,
    )
    if matching_index is None:
        raise FormSecurityError("invalid checkout nonce")

    del active[matching_index]
    session[_CHECKOUT_NONCES_SESSION_KEY] = active
