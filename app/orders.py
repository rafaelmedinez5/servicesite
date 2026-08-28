from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from app.catalog import PurchasableService


MAX_CART_SERVICES = 20
MAX_ITEM_QUANTITY = 10


class CartError(ValueError):
    """The requested cart operation is invalid."""


class CartChangedError(CartError):
    """The cart or its current catalog prices need to be reviewed again."""


class CheckoutInProgressError(CartError):
    """Another request is already creating an invoice for this cart revision."""


@dataclass(frozen=True)
class OrderLine:
    service: PurchasableService
    quantity: int

    def __post_init__(self) -> None:
        if not isinstance(self.service, PurchasableService):
            raise CartError("a service snapshot is required")
        validate_quantity(self.quantity)

    @property
    def total_usd_cents(self) -> int:
        return self.service.price_usd_cents * self.quantity


@dataclass(frozen=True)
class CartItem:
    service_id: str
    name: str
    slug: str
    quantity: int
    service: PurchasableService | None

    @property
    def total_usd_cents(self) -> int:
        return self.service.price_usd_cents * self.quantity if self.service else 0


@dataclass(frozen=True)
class CartSnapshot:
    version: int
    items: tuple[CartItem, ...]

    @property
    def ready(self) -> bool:
        return bool(self.items) and all(item.service is not None for item in self.items)

    @property
    def total_usd_cents(self) -> int:
        return sum(item.total_usd_cents for item in self.items)

    @property
    def quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            [asdict(item) for item in self.items], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def lines(self) -> tuple[OrderLine, ...]:
        if not self.ready:
            raise CartChangedError("the cart contains unavailable services or is empty")
        return tuple(OrderLine(item.service, item.quantity) for item in self.items)


def validate_quantity(value: int, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_ITEM_QUANTITY:
        raise CartError(f"quantity must be between {minimum} and {MAX_ITEM_QUANTITY}")
