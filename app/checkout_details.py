from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping


MAX_REQUEST_LENGTH = 4000
MAX_CHECKOUT_BODY_BYTES = 384 * 1024
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+")
_DOMAIN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TELEGRAM = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")


class CheckoutValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("Check the highlighted checkout fields.")
        self.errors = errors


def validate_delivery(method: str, address: str) -> str:
    if method not in {"account", "email", "telegram"}:
        raise ValueError("Choose My account, email or Telegram.")
    if method == "account":
        return ""  # Do not retain an external contact for account delivery.
    address = address.strip()
    if method == "telegram":
        username = address.removeprefix("@")
        if not _TELEGRAM.fullmatch(username):
            raise ValueError("Enter a Telegram username: 5–32 letters, numbers or underscores, starting with a letter.")
        return "@" + username
    if len(address) > 254 or address.count("@") != 1:
        raise ValueError("Enter a valid email address, such as name@example.com.")
    local, domain = address.rsplit("@", 1)
    labels = domain.split(".")
    if (
        not 1 <= len(local) <= 64 or not _EMAIL_LOCAL.fullmatch(local)
        or local.startswith(".") or local.endswith(".") or ".." in local
        or len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
        or not re.fullmatch(r"[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59}", labels[-1])
    ):
        raise ValueError("Enter a valid email address, such as name@example.com.")
    return local + "@" + domain.lower()


def validate_request(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > MAX_REQUEST_LENGTH:
        raise ValueError(f"Keep your request within {MAX_REQUEST_LENGTH:,} characters.")
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
        raise ValueError("Remove unsupported control characters from your request.")
    return value


@dataclass(frozen=True)
class CheckoutDetails:
    delivery_method: str
    delivery_address: str = field(repr=False)
    item_requests: tuple[tuple[str, str], ...] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_address", validate_delivery(
            self.delivery_method, self.delivery_address
        ))
        if not 1 <= len(self.item_requests) <= 20:
            raise ValueError("Each order item needs a request entry, which may be blank.")
        normalized = tuple((service_id, validate_request(text)) for service_id, text in self.item_requests)
        if len({service_id for service_id, _ in normalized}) != len(normalized):
            raise ValueError("Each order item must have exactly one request.")
        object.__setattr__(self, "item_requests", normalized)

    @property
    def requests_by_service(self) -> dict[str, str]:
        return dict(self.item_requests)

    def require_services(self, service_ids: tuple[str, ...]) -> None:
        if set(self.requests_by_service) != set(service_ids):
            raise ValueError("Requests must match the reviewed order items.")


def parse_checkout_details(values: Mapping[str, str], service_ids: tuple[str, ...]) -> CheckoutDetails:
    errors = {}
    method = values.get("delivery_method", "")
    address = values.get("delivery_address", "")
    try:
        address = validate_delivery(method, address)
    except ValueError as exc:
        errors["delivery_method" if method not in {"account", "email", "telegram"} else "delivery_address"] = str(exc)
    item_requests = []
    for service_id in service_ids:
        name = "request_" + service_id
        try:
            item_requests.append((service_id, validate_request(values.get(name, ""))))
        except ValueError as exc:
            errors[name] = str(exc)
    if errors:
        raise CheckoutValidationError(errors)
    return CheckoutDetails(method, address, tuple(item_requests))
