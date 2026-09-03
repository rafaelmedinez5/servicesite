from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


MAX_DELIVERY_LENGTH = 12000
MAX_DELIVERY_BODY_BYTES = 64 * 1024


class DeliveryValidationError(ValueError):
    """A customer-facing delivery cannot be published as submitted."""


@dataclass(frozen=True)
class AccountDelivery:
    body: str = field(repr=False)
    delivered_at: datetime


def validate_delivery_body(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > MAX_DELIVERY_LENGTH:
        raise DeliveryValidationError("Enter the account delivery in 1–12,000 characters.")
    if any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value):
        raise DeliveryValidationError("Remove unsupported control characters from the delivery.")
    return value
