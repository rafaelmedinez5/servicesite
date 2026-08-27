from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


_SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class CatalogValidationError(ValueError):
    """Catalog input is not safe to persist or sell."""


def _require_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise CatalogValidationError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise CatalogValidationError(f"{label} must contain 1 through {maximum} characters")
    return normalized


def _require_aware_datetime(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CatalogValidationError(f"{label} must be timezone-aware")


@dataclass(frozen=True)
class CategoryRecord:
    id: str
    name: str
    slug: str
    description: str
    published: bool
    archived: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.id, "category ID", maximum=64)
        _require_text(self.name, "category name", maximum=200)
        slug = _require_text(self.slug, "category slug", maximum=120)
        if not _SLUG_PATTERN.fullmatch(slug):
            raise CatalogValidationError("category slug must use lowercase letters, digits, and hyphens")
        if not isinstance(self.description, str) or len(self.description) > 10_000:
            raise CatalogValidationError("category description is too long")
        if not isinstance(self.published, bool) or not isinstance(self.archived, bool):
            raise CatalogValidationError("category publication flags must be boolean")
        if isinstance(self.sort_order, bool) or not isinstance(self.sort_order, int):
            raise CatalogValidationError("category sort order must be an integer")
        _require_aware_datetime(self.created_at, "category created_at")
        _require_aware_datetime(self.updated_at, "category updated_at")


@dataclass(frozen=True)
class ServiceRecord:
    id: str
    category_id: str
    name: str
    slug: str
    description: str
    price_usd_cents: int
    duration_label: str | None
    published: bool
    archived: bool
    sort_order: int
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_text(self.id, "service ID", maximum=64)
        _require_text(self.category_id, "category ID", maximum=64)
        _require_text(self.name, "service name", maximum=200)
        slug = _require_text(self.slug, "service slug", maximum=120)
        if not _SLUG_PATTERN.fullmatch(slug):
            raise CatalogValidationError("service slug must use lowercase letters, digits, and hyphens")
        if not isinstance(self.description, str) or len(self.description) > 20_000:
            raise CatalogValidationError("service description is too long")
        if (
            isinstance(self.price_usd_cents, bool)
            or not isinstance(self.price_usd_cents, int)
            or self.price_usd_cents <= 0
        ):
            raise CatalogValidationError("service USD price must be a positive integer number of cents")
        if self.duration_label is not None:
            _require_text(self.duration_label, "service duration", maximum=200)
        if not isinstance(self.published, bool) or not isinstance(self.archived, bool):
            raise CatalogValidationError("service publication flags must be boolean")
        if isinstance(self.sort_order, bool) or not isinstance(self.sort_order, int):
            raise CatalogValidationError("service sort order must be an integer")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise CatalogValidationError("service version must be a positive integer")
        _require_aware_datetime(self.created_at, "service created_at")
        _require_aware_datetime(self.updated_at, "service updated_at")


@dataclass(frozen=True)
class PurchasableService:
    service_id: str
    service_slug: str
    service_version: int
    service_name: str
    service_description: str
    duration_label: str | None
    category_id: str
    category_name: str
    category_description: str
    price_usd_cents: int


@dataclass(frozen=True)
class AdminServiceRecord:
    service: ServiceRecord
    category_name: str
