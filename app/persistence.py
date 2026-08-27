from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from app.catalog import AdminServiceRecord, CategoryRecord, PurchasableService, ServiceRecord
from app.payments.invoice import (
    Invoice,
    InvoiceValidationError,
    PaymentStatus,
    transition_invoice,
)
from app.payments.xmr_wallet_rpc import MAX_ATOMIC_UNITS


SCHEMA_VERSION = 4


class PersistenceError(RuntimeError):
    """Base class for sanitized database failures."""


class DatabaseConfigurationError(PersistenceError):
    """The selected database path is unsafe or unusable."""


class DatabaseNotFreshError(PersistenceError):
    """The database contains unrecognized pre-existing application tables."""


class SchemaVersionError(PersistenceError):
    """The database schema version is not supported by this application."""


class InvoiceNotFoundError(PersistenceError):
    """The requested invoice does not exist."""


class CatalogChangedError(PersistenceError):
    """The service changed after checkout validation but before persistence."""


class InvoicePersistenceError(PersistenceError):
    """An invoice could not be stored without violating an invariant."""


class FulfillmentNotAllowedError(PersistenceError):
    """A purchase cannot be fulfilled before payment settlement."""


class FulfillmentStatus(str, Enum):
    UNFULFILLED = "unfulfilled"
    FULFILLED = "fulfilled"


@dataclass(frozen=True)
class AdminCredential:
    password_hash: str = field(repr=False)
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AdminPurchaseRecord:
    invoice_id: str
    service_id: str
    service_name: str
    category_id: str
    category_name: str
    price_usd_cents: int
    expected_atomic: int
    observed_atomic: int
    required_confirmations: int
    observed_confirmations: int
    payment_status: PaymentStatus
    fulfillment_status: FulfillmentStatus
    fulfillment_note: str
    created_at: datetime
    expires_at: datetime
    settled_at: datetime | None
    fulfilled_at: datetime | None


@dataclass(frozen=True)
class SweepAttempt:
    invoice_id: str
    attempt_token: str
    started_at: datetime
    uncertain: bool
    updated_at: datetime


class SQLiteDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(path) == ":memory:":
            raise DatabaseConfigurationError("a persistent SQLite path is required")

    def initialize(self) -> None:
        if not self.path.parent.is_dir():
            raise DatabaseConfigurationError("database parent directory does not exist")

        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            existing_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if existing_tables and "schema_meta" not in existing_tables:
                raise DatabaseNotFreshError(
                    "database is not an empty servicesite database; import is refused"
                )

            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                previous_version = version_row["value"] if version_row is not None else None
                if previous_version is None:
                    connection.execute(
                        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif previous_version not in {"1", "2", "3", str(SCHEMA_VERSION)}:
                    raise SchemaVersionError("database schema version is unsupported")

                if previous_version in {"1", "2"}:
                    _ensure_fulfillment_columns(connection)
                connection.executescript(_SCHEMA_SQL)
                if previous_version in {"1", "2", "3"}:
                    connection.execute(
                        "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                        (str(SCHEMA_VERSION),),
                    )
        finally:
            connection.close()

        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class ServicesiteRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def insert_category(self, category: CategoryRecord) -> None:
        if not isinstance(category, CategoryRecord):
            raise TypeError("category must be a validated CategoryRecord")
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO categories(
                        id, name, slug, description, published, archived, sort_order,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        category.id,
                        category.name,
                        category.slug,
                        category.description,
                        int(category.published),
                        int(category.archived),
                        category.sort_order,
                        _serialize_datetime(category.created_at),
                        _serialize_datetime(category.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceError("category could not be stored") from exc

    def insert_service(self, service: ServiceRecord) -> None:
        if not isinstance(service, ServiceRecord):
            raise TypeError("service must be a validated ServiceRecord")
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO services(
                        id, category_id, name, slug, description, price_usd_cents,
                        duration_label, published, archived, sort_order, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service.id,
                        service.category_id,
                        service.name,
                        service.slug,
                        service.description,
                        service.price_usd_cents,
                        service.duration_label,
                        int(service.published),
                        int(service.archived),
                        service.sort_order,
                        service.version,
                        _serialize_datetime(service.created_at),
                        _serialize_datetime(service.updated_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceError("service could not be stored") from exc

    def list_categories(self, *, include_archived: bool = True) -> list[CategoryRecord]:
        connection = self.database.connect()
        try:
            where = "" if include_archived else "WHERE archived=0"
            rows = connection.execute(
                f"SELECT * FROM categories {where} "
                "ORDER BY archived, sort_order, name COLLATE NOCASE"
            ).fetchall()
            return [_row_to_category(row) for row in rows]
        finally:
            connection.close()

    def get_category(self, category_id: str) -> CategoryRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM categories WHERE id=?", (category_id,)
            ).fetchone()
            return _row_to_category(row) if row is not None else None
        finally:
            connection.close()

    def update_category(self, category: CategoryRecord) -> None:
        if not isinstance(category, CategoryRecord):
            raise TypeError("category must be a validated CategoryRecord")
        try:
            with self.database.transaction(immediate=True) as connection:
                result = connection.execute(
                    """
                    UPDATE categories
                    SET name=?, slug=?, description=?, published=?, sort_order=?, updated_at=?
                    WHERE id=? AND archived=0
                    """,
                    (
                        category.name,
                        category.slug,
                        category.description,
                        int(category.published),
                        category.sort_order,
                        _serialize_datetime(category.updated_at),
                        category.id,
                    ),
                )
                if result.rowcount != 1:
                    raise PersistenceError("category was not found or is archived")
        except sqlite3.IntegrityError as exc:
            raise PersistenceError("category could not be updated") from exc

    def archive_category(self, category_id: str, *, now: datetime) -> None:
        _require_aware_datetime(now, "category archive time")
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute(
                """
                UPDATE categories
                SET published=0, archived=1, updated_at=?
                WHERE id=? AND archived=0
                """,
                (_serialize_datetime(now), category_id),
            )
            if result.rowcount != 1:
                raise PersistenceError("category was not found or is already archived")

    def list_services(self, *, include_archived: bool = True) -> list[AdminServiceRecord]:
        connection = self.database.connect()
        try:
            where = "" if include_archived else "WHERE s.archived=0"
            rows = connection.execute(
                f"""
                SELECT s.*, c.name AS category_name
                FROM services AS s
                JOIN categories AS c ON c.id=s.category_id
                {where}
                ORDER BY s.archived, c.sort_order, c.name COLLATE NOCASE,
                         s.sort_order, s.name COLLATE NOCASE
                """
            ).fetchall()
            return [
                AdminServiceRecord(
                    service=_row_to_service(row), category_name=row["category_name"]
                )
                for row in rows
            ]
        finally:
            connection.close()

    def get_service(self, service_id: str) -> ServiceRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM services WHERE id=?", (service_id,)
            ).fetchone()
            return _row_to_service(row) if row is not None else None
        finally:
            connection.close()

    def update_service(self, service: ServiceRecord) -> None:
        if not isinstance(service, ServiceRecord):
            raise TypeError("service must be a validated ServiceRecord")
        try:
            with self.database.transaction(immediate=True) as connection:
                result = connection.execute(
                    """
                    UPDATE services
                    SET category_id=?, name=?, slug=?, description=?,
                        price_usd_cents=?, duration_label=?, published=?,
                        sort_order=?, version=version+1, updated_at=?
                    WHERE id=? AND archived=0
                    """,
                    (
                        service.category_id,
                        service.name,
                        service.slug,
                        service.description,
                        service.price_usd_cents,
                        service.duration_label,
                        int(service.published),
                        service.sort_order,
                        _serialize_datetime(service.updated_at),
                        service.id,
                    ),
                )
                if result.rowcount != 1:
                    raise PersistenceError("service was not found or is archived")
        except sqlite3.IntegrityError as exc:
            raise PersistenceError("service could not be updated") from exc

    def archive_service(self, service_id: str, *, now: datetime) -> None:
        _require_aware_datetime(now, "service archive time")
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute(
                """
                UPDATE services
                SET published=0, archived=1, version=version+1, updated_at=?
                WHERE id=? AND archived=0
                """,
                (_serialize_datetime(now), service_id),
            )
            if result.rowcount != 1:
                raise PersistenceError("service was not found or is already archived")

    def update_service_price(
        self, service_id: str, *, price_usd_cents: int, now: datetime
    ) -> None:
        _require_positive_int(price_usd_cents, "USD price cents")
        _require_aware_datetime(now, "service update time")
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute(
                """
                UPDATE services
                SET price_usd_cents=?, version=version+1, updated_at=?
                WHERE id=?
                """,
                (price_usd_cents, _serialize_datetime(now), service_id),
            )
            if result.rowcount != 1:
                raise PersistenceError("service was not found")

    def get_purchasable_service(self, service_id: str) -> PurchasableService | None:
        connection = self.database.connect()
        try:
            return _fetch_purchasable_service(connection, service_id)
        finally:
            connection.close()

    def get_purchasable_service_by_slug(
        self, service_slug: str
    ) -> PurchasableService | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT
                    s.id AS service_id,
                    s.slug AS service_slug,
                    s.version AS service_version,
                    s.name AS service_name,
                    s.description AS service_description,
                    s.duration_label,
                    s.price_usd_cents,
                    c.id AS category_id,
                    c.name AS category_name,
                    c.description AS category_description
                FROM services AS s
                JOIN categories AS c ON c.id=s.category_id
                WHERE s.slug=?
                  AND s.published=1 AND s.archived=0
                  AND c.published=1 AND c.archived=0
                """,
                (service_slug,),
            ).fetchone()
            if row is None:
                return None
            return _row_to_purchasable_service(row)
        finally:
            connection.close()

    def list_purchasable_services(self) -> list[PurchasableService]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    s.id AS service_id,
                    s.slug AS service_slug,
                    s.version AS service_version,
                    s.name AS service_name,
                    s.description AS service_description,
                    s.duration_label,
                    s.price_usd_cents,
                    c.id AS category_id,
                    c.name AS category_name,
                    c.description AS category_description
                FROM services AS s
                JOIN categories AS c ON c.id=s.category_id
                WHERE s.published=1 AND s.archived=0
                  AND c.published=1 AND c.archived=0
                ORDER BY c.sort_order, c.name COLLATE NOCASE,
                         s.sort_order, s.name COLLATE NOCASE
                """
            ).fetchall()
            return [_row_to_purchasable_service(row) for row in rows]
        finally:
            connection.close()

    def insert_invoice(
        self, invoice: Invoice, expected_service: PurchasableService
    ) -> None:
        if not isinstance(invoice, Invoice) or not isinstance(
            expected_service, PurchasableService
        ):
            raise TypeError("validated invoice and service snapshot are required")
        try:
            with self.database.transaction(immediate=True) as connection:
                current_service = _fetch_purchasable_service(
                    connection, expected_service.service_id
                )
                if current_service != expected_service:
                    raise CatalogChangedError(
                        "service changed before the invoice could be stored"
                    )
                connection.execute(
                    """
                    INSERT INTO invoices(
                        id, status_token, service_id, service_version,
                        service_name_snapshot, service_description_snapshot,
                        duration_label_snapshot, category_id, category_name_snapshot,
                        category_description_snapshot, price_usd_cents, xmr_usd_rate,
                        rate_source, quote_created_at, expected_atomic, observed_atomic,
                        xmr_address, xmr_account_index, xmr_address_index,
                        required_confirmations, observed_confirmations, deposit_txid,
                        sweep_txid, sweep_required, status, status_note, created_at,
                        expires_at, expired_at, settled_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    _invoice_values(invoice),
                )
        except CatalogChangedError:
            raise
        except sqlite3.IntegrityError as exc:
            raise InvoicePersistenceError(
                "invoice could not be stored without violating an invariant"
            ) from exc

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id=?", (invoice_id,)
            ).fetchone()
            return _row_to_invoice(row) if row is not None else None
        finally:
            connection.close()

    def get_invoice_by_token(self, invoice_id: str, status_token: str) -> Invoice | None:
        if not isinstance(status_token, str):
            return None
        invoice = self.get_invoice(invoice_id)
        if invoice is None or not secrets.compare_digest(invoice.status_token, status_token):
            return None
        return invoice

    def count_invoices(self) -> int:
        connection = self.database.connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0])
        finally:
            connection.close()

    def list_admin_purchases(
        self,
        *,
        category_id: str | None = None,
        service_id: str | None = None,
        payment_status: PaymentStatus | None = None,
        fulfillment_status: FulfillmentStatus | None = None,
        created_from: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 500,
    ) -> list[AdminPurchaseRecord]:
        if created_from is not None:
            _require_aware_datetime(created_from, "purchase filter start")
        if created_before is not None:
            _require_aware_datetime(created_before, "purchase filter end")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise PersistenceError("purchase result limit is invalid")

        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (("category_id", category_id), ("service_id", service_id)):
            if value:
                clauses.append(f"{column}=?")
                parameters.append(value)
        if payment_status is not None:
            clauses.append("status=?")
            parameters.append(payment_status.value)
        if fulfillment_status is not None:
            clauses.append("fulfillment_status=?")
            parameters.append(fulfillment_status.value)
        if created_from is not None:
            clauses.append("created_at>=?")
            parameters.append(_serialize_datetime(created_from))
        if created_before is not None:
            clauses.append("created_at<?")
            parameters.append(_serialize_datetime(created_before))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)

        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM invoices
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [_row_to_admin_purchase(row) for row in rows]
        finally:
            connection.close()

    def get_admin_purchase(self, invoice_id: str) -> AdminPurchaseRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id=?", (invoice_id,)
            ).fetchone()
            return _row_to_admin_purchase(row) if row is not None else None
        finally:
            connection.close()

    def mark_purchase_fulfilled(
        self, invoice_id: str, *, note: str, now: datetime
    ) -> AdminPurchaseRecord:
        _require_aware_datetime(now, "fulfillment time")
        if not isinstance(note, str) or len(note.strip()) > 2_000:
            raise PersistenceError("fulfillment note is too long")
        normalized_note = note.strip()
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM invoices WHERE id=?", (invoice_id,)
            ).fetchone()
            if row is None:
                raise InvoiceNotFoundError("invoice was not found")
            if PaymentStatus(row["status"]) is not PaymentStatus.SETTLED:
                raise FulfillmentNotAllowedError(
                    "purchase payment must be settled before fulfillment"
                )
            if FulfillmentStatus(row["fulfillment_status"]) is FulfillmentStatus.FULFILLED:
                return _row_to_admin_purchase(row)
            connection.execute(
                """
                UPDATE invoices
                SET fulfillment_status='fulfilled', fulfillment_note=?, fulfilled_at=?
                WHERE id=? AND status='settled' AND fulfillment_status='unfulfilled'
                """,
                (normalized_note, _serialize_datetime(now), invoice_id),
            )
            updated = connection.execute(
                "SELECT * FROM invoices WHERE id=?", (invoice_id,)
            ).fetchone()
            return _row_to_admin_purchase(updated)

    def admin_login_allowed(self, *, now: datetime) -> bool:
        _require_aware_datetime(now, "login attempt time")
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT blocked_until FROM admin_login_guard WHERE id=1"
            ).fetchone()
            return (
                row is None
                or row["blocked_until"] is None
                or _parse_datetime(row["blocked_until"]) <= now
            )
        finally:
            connection.close()

    def record_admin_login_failure(
        self,
        *,
        now: datetime,
        maximum_attempts: int = 5,
        window: timedelta = timedelta(minutes=15),
        block_for: timedelta = timedelta(minutes=15),
    ) -> None:
        _require_aware_datetime(now, "login failure time")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM admin_login_guard WHERE id=1"
            ).fetchone()
            if row is None or _parse_datetime(row["window_started_at"]) + window <= now:
                window_started_at = now
                failure_count = 1
            else:
                window_started_at = _parse_datetime(row["window_started_at"])
                failure_count = int(row["failure_count"]) + 1
            blocked_until = now + block_for if failure_count >= maximum_attempts else None
            connection.execute(
                """
                INSERT INTO admin_login_guard(
                    id, window_started_at, failure_count, blocked_until
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    window_started_at=excluded.window_started_at,
                    failure_count=excluded.failure_count,
                    blocked_until=excluded.blocked_until
                """,
                (
                    _serialize_datetime(window_started_at),
                    failure_count,
                    _serialize_optional_datetime(blocked_until),
                ),
            )

    def clear_admin_login_failures(self) -> None:
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM admin_login_guard WHERE id=1")

    def get_admin_credential(self) -> AdminCredential | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT password_hash, credential_version, created_at, updated_at "
                "FROM admin_credentials WHERE id=1"
            ).fetchone()
            return _row_to_admin_credential(row) if row is not None else None
        finally:
            connection.close()

    def create_admin_credential(self, password_hash: str, *, now: datetime) -> bool:
        _validate_admin_password_hash(password_hash)
        _require_aware_datetime(now, "admin credential creation time")
        serialized_now = _serialize_datetime(now)
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO admin_credentials(
                    id, password_hash, credential_version, created_at, updated_at
                ) VALUES (1, ?, 1, ?, ?)
                """,
                (password_hash, serialized_now, serialized_now),
            )
            return cursor.rowcount == 1

    def replace_admin_password_hash(
        self,
        *,
        expected_hash: str,
        new_hash: str,
        now: datetime,
    ) -> bool:
        _validate_admin_password_hash(expected_hash)
        _validate_admin_password_hash(new_hash)
        _require_aware_datetime(now, "admin password change time")
        with self.database.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE admin_credentials
                SET password_hash=?,
                    credential_version=credential_version + 1,
                    updated_at=?
                WHERE id=1 AND password_hash=?
                """,
                (new_hash, _serialize_datetime(now), expected_hash),
            )
            return cursor.rowcount == 1

    def list_open_invoices(self) -> list[Invoice]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM invoices
                WHERE status IN (
                    'awaiting_payment',
                    'paid_pending_confirmations',
                    'paid_pending_sweep',
                    'sweeping_to_cold'
                )
                ORDER BY created_at, id
                """
            ).fetchall()
            return [_row_to_invoice(row) for row in rows]
        finally:
            connection.close()

    def claim_invoice(
        self,
        invoice_id: str,
        *,
        claim_token: str,
        claimed_at: datetime,
        expires_at: datetime,
    ) -> bool:
        _require_token(claim_token, "poll claim token")
        _require_aware_datetime(claimed_at, "poll claim time")
        _require_aware_datetime(expires_at, "poll claim expiry")
        if expires_at <= claimed_at:
            raise InvoiceValidationError("poll claim expiry must follow claim time")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM invoice_poll_claims WHERE expires_at<=?",
                (_serialize_datetime(claimed_at),),
            )
            result = connection.execute(
                """
                INSERT OR IGNORE INTO invoice_poll_claims(
                    invoice_id, claim_token, claimed_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    invoice_id,
                    claim_token,
                    _serialize_datetime(claimed_at),
                    _serialize_datetime(expires_at),
                ),
            )
            return result.rowcount == 1

    def release_invoice_claim(self, invoice_id: str, *, claim_token: str) -> None:
        _require_token(claim_token, "poll claim token")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM invoice_poll_claims WHERE invoice_id=? AND claim_token=?",
                (invoice_id, claim_token),
            )

    def get_sweep_attempt(self, invoice_id: str) -> SweepAttempt | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM invoice_sweep_attempts WHERE invoice_id=?",
                (invoice_id,),
            ).fetchone()
            if row is None:
                return None
            return SweepAttempt(
                invoice_id=row["invoice_id"],
                attempt_token=row["attempt_token"],
                started_at=_parse_datetime(row["started_at"]),
                uncertain=bool(row["uncertain"]),
                updated_at=_parse_datetime(row["updated_at"]),
            )
        finally:
            connection.close()

    def claim_sweep(
        self,
        invoice_id: str,
        *,
        attempt_token: str,
        now: datetime,
    ) -> Invoice | None:
        _require_token(attempt_token, "sweep attempt token")
        _require_aware_datetime(now, "sweep claim time")
        with self.database.transaction(immediate=True) as connection:
            current = _require_invoice(connection, invoice_id)
            if current.status is not PaymentStatus.PAID_PENDING_SWEEP:
                return None
            if current.sweep_txid:
                return None
            transitioned = transition_invoice(
                current, PaymentStatus.SWEEPING_TO_COLD, now=now
            )
            connection.execute(
                """
                UPDATE invoices SET status=?, status_note=?, updated_at=?
                WHERE id=? AND status='paid_pending_sweep' AND sweep_txid IS NULL
                """,
                (
                    transitioned.status.value,
                    transitioned.status_note,
                    _serialize_datetime(transitioned.updated_at),
                    invoice_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO invoice_sweep_attempts(
                    invoice_id, attempt_token, started_at, uncertain, updated_at
                ) VALUES (?, ?, ?, 0, ?)
                """,
                (
                    invoice_id,
                    attempt_token,
                    _serialize_datetime(now),
                    _serialize_datetime(now),
                ),
            )
            return _require_invoice(connection, invoice_id)

    def mark_sweep_uncertain(
        self, invoice_id: str, *, attempt_token: str, now: datetime
    ) -> None:
        _require_token(attempt_token, "sweep attempt token")
        _require_aware_datetime(now, "sweep update time")
        with self.database.transaction(immediate=True) as connection:
            result = connection.execute(
                """
                UPDATE invoice_sweep_attempts
                SET uncertain=1, updated_at=?
                WHERE invoice_id=? AND attempt_token=?
                """,
                (_serialize_datetime(now), invoice_id, attempt_token),
            )
            if result.rowcount != 1:
                raise InvoicePersistenceError("sweep attempt claim was lost")

    def record_claimed_sweep_transaction(
        self,
        invoice_id: str,
        *,
        attempt_token: str,
        sweep_txid: str,
        now: datetime,
    ) -> Invoice:
        _require_token(attempt_token, "sweep attempt token")
        if not isinstance(sweep_txid, str) or not sweep_txid or len(sweep_txid) > 200:
            raise InvoiceValidationError("sweep transaction ID is invalid")
        _require_aware_datetime(now, "sweep record time")
        with self.database.transaction(immediate=True) as connection:
            attempt = connection.execute(
                """
                SELECT attempt_token FROM invoice_sweep_attempts
                WHERE invoice_id=?
                """,
                (invoice_id,),
            ).fetchone()
            if attempt is None or not secrets.compare_digest(
                attempt["attempt_token"], attempt_token
            ):
                raise InvoicePersistenceError("sweep attempt claim was lost")
            current = _require_invoice(connection, invoice_id)
            if current.status is not PaymentStatus.SWEEPING_TO_COLD:
                raise InvoicePersistenceError("invoice is not in a sweep attempt")
            if current.sweep_txid and current.sweep_txid != sweep_txid:
                raise InvoicePersistenceError("a different sweep transaction is already recorded")
            connection.execute(
                """
                UPDATE invoices
                SET sweep_txid=COALESCE(sweep_txid, ?), updated_at=?
                WHERE id=?
                """,
                (sweep_txid, _serialize_datetime(now), invoice_id),
            )
            connection.execute(
                "DELETE FROM invoice_sweep_attempts WHERE invoice_id=?",
                (invoice_id,),
            )
            return _require_invoice(connection, invoice_id)

    def release_sweep_attempt(
        self, invoice_id: str, *, attempt_token: str, now: datetime
    ) -> Invoice:
        _require_token(attempt_token, "sweep attempt token")
        _require_aware_datetime(now, "sweep release time")
        with self.database.transaction(immediate=True) as connection:
            attempt = connection.execute(
                "SELECT attempt_token FROM invoice_sweep_attempts WHERE invoice_id=?",
                (invoice_id,),
            ).fetchone()
            if attempt is None or not secrets.compare_digest(
                attempt["attempt_token"], attempt_token
            ):
                raise InvoicePersistenceError("sweep attempt claim was lost")
            current = _require_invoice(connection, invoice_id)
            transitioned = transition_invoice(
                current, PaymentStatus.PAID_PENDING_SWEEP, now=now
            )
            connection.execute(
                """
                UPDATE invoices SET status=?, status_note=?, updated_at=?
                WHERE id=? AND status='sweeping_to_cold'
                """,
                (
                    transitioned.status.value,
                    transitioned.status_note,
                    _serialize_datetime(transitioned.updated_at),
                    invoice_id,
                ),
            )
            connection.execute(
                "DELETE FROM invoice_sweep_attempts WHERE invoice_id=?",
                (invoice_id,),
            )
            return _require_invoice(connection, invoice_id)

    def record_observation(
        self,
        invoice_id: str,
        *,
        observed_atomic: int,
        observed_confirmations: int,
        deposit_txid: str | None,
        now: datetime,
    ) -> Invoice:
        _require_non_negative_int(observed_atomic, "observed atomic amount")
        _require_non_negative_int(observed_confirmations, "observed confirmations")
        if observed_atomic > MAX_ATOMIC_UNITS:
            raise InvoiceValidationError("observed atomic amount exceeds storage range")
        if deposit_txid is not None and (not isinstance(deposit_txid, str) or not deposit_txid):
            raise InvoiceValidationError("deposit transaction ID must be non-empty")
        _require_aware_datetime(now, "observation time")

        with self.database.transaction(immediate=True) as connection:
            current = _require_invoice(connection, invoice_id)
            if now < current.updated_at:
                raise InvoiceValidationError("observation time cannot move backwards")
            connection.execute(
                """
                UPDATE invoices
                SET observed_atomic=?, observed_confirmations=?,
                    deposit_txid=COALESCE(deposit_txid, ?), updated_at=?
                WHERE id=?
                """,
                (
                    observed_atomic,
                    observed_confirmations,
                    deposit_txid,
                    _serialize_datetime(now),
                    invoice_id,
                ),
            )
            return _require_invoice(connection, invoice_id)

    def record_sweep_transaction(
        self, invoice_id: str, *, sweep_txid: str, now: datetime
    ) -> Invoice:
        if not isinstance(sweep_txid, str) or not sweep_txid:
            raise InvoiceValidationError("sweep transaction ID must be non-empty")
        _require_aware_datetime(now, "sweep record time")
        with self.database.transaction(immediate=True) as connection:
            current = _require_invoice(connection, invoice_id)
            if current.sweep_txid and current.sweep_txid != sweep_txid:
                raise InvoicePersistenceError("a different sweep transaction is already recorded")
            if now < current.updated_at:
                raise InvoiceValidationError("sweep record time cannot move backwards")
            connection.execute(
                "UPDATE invoices SET sweep_txid=COALESCE(sweep_txid, ?), updated_at=? WHERE id=?",
                (sweep_txid, _serialize_datetime(now), invoice_id),
            )
            return _require_invoice(connection, invoice_id)

    def transition_status(
        self, invoice_id: str, new_status: PaymentStatus, *, now: datetime
    ) -> Invoice:
        with self.database.transaction(immediate=True) as connection:
            current = _require_invoice(connection, invoice_id)
            transitioned = transition_invoice(current, new_status, now=now)
            result = connection.execute(
                """
                UPDATE invoices
                SET status=?, status_note=?, expired_at=?, settled_at=?, updated_at=?
                WHERE id=? AND status=?
                """,
                (
                    transitioned.status.value,
                    transitioned.status_note,
                    _serialize_optional_datetime(transitioned.expired_at),
                    _serialize_optional_datetime(transitioned.settled_at),
                    _serialize_datetime(transitioned.updated_at),
                    invoice_id,
                    current.status.value,
                ),
            )
            if result.rowcount != 1:
                raise InvoicePersistenceError("invoice status changed concurrently")
            return _require_invoice(connection, invoice_id)


def _fetch_purchasable_service(
    connection: sqlite3.Connection, service_id: str
) -> PurchasableService | None:
    row = connection.execute(
        """
        SELECT
            s.id AS service_id,
            s.slug AS service_slug,
            s.version AS service_version,
            s.name AS service_name,
            s.description AS service_description,
            s.duration_label,
            s.price_usd_cents,
            c.id AS category_id,
            c.name AS category_name,
            c.description AS category_description
        FROM services AS s
        JOIN categories AS c ON c.id=s.category_id
        WHERE s.id=?
          AND s.published=1 AND s.archived=0
          AND c.published=1 AND c.archived=0
        """,
        (service_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_purchasable_service(row)


def _row_to_purchasable_service(row: sqlite3.Row) -> PurchasableService:
    return PurchasableService(
        service_id=row["service_id"],
        service_slug=row["service_slug"],
        service_version=int(row["service_version"]),
        service_name=row["service_name"],
        service_description=row["service_description"],
        duration_label=row["duration_label"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        category_description=row["category_description"],
        price_usd_cents=int(row["price_usd_cents"]),
    )


def _row_to_category(row: sqlite3.Row) -> CategoryRecord:
    return CategoryRecord(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        published=bool(row["published"]),
        archived=bool(row["archived"]),
        sort_order=int(row["sort_order"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_service(row: sqlite3.Row) -> ServiceRecord:
    return ServiceRecord(
        id=row["id"],
        category_id=row["category_id"],
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        price_usd_cents=int(row["price_usd_cents"]),
        duration_label=row["duration_label"],
        published=bool(row["published"]),
        archived=bool(row["archived"]),
        sort_order=int(row["sort_order"]),
        version=int(row["version"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _row_to_admin_purchase(row: sqlite3.Row) -> AdminPurchaseRecord:
    return AdminPurchaseRecord(
        invoice_id=row["id"],
        service_id=row["service_id"],
        service_name=row["service_name_snapshot"],
        category_id=row["category_id"],
        category_name=row["category_name_snapshot"],
        price_usd_cents=int(row["price_usd_cents"]),
        expected_atomic=int(row["expected_atomic"]),
        observed_atomic=int(row["observed_atomic"]),
        required_confirmations=int(row["required_confirmations"]),
        observed_confirmations=int(row["observed_confirmations"]),
        payment_status=PaymentStatus(row["status"]),
        fulfillment_status=FulfillmentStatus(row["fulfillment_status"]),
        fulfillment_note=row["fulfillment_note"],
        created_at=_parse_datetime(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        settled_at=_parse_optional_datetime(row["settled_at"]),
        fulfilled_at=_parse_optional_datetime(row["fulfilled_at"]),
    )


def _row_to_admin_credential(row: sqlite3.Row) -> AdminCredential:
    return AdminCredential(
        password_hash=row["password_hash"],
        version=int(row["credential_version"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _require_invoice(connection: sqlite3.Connection, invoice_id: str) -> Invoice:
    row = connection.execute(
        "SELECT * FROM invoices WHERE id=?", (invoice_id,)
    ).fetchone()
    if row is None:
        raise InvoiceNotFoundError("invoice was not found")
    return _row_to_invoice(row)


def _invoice_values(invoice: Invoice) -> tuple[object, ...]:
    return (
        invoice.id,
        invoice.status_token,
        invoice.service_id,
        invoice.service_version,
        invoice.service_name_snapshot,
        invoice.service_description_snapshot,
        invoice.duration_label_snapshot,
        invoice.category_id,
        invoice.category_name_snapshot,
        invoice.category_description_snapshot,
        invoice.price_usd_cents,
        invoice.xmr_usd_rate,
        invoice.rate_source,
        _serialize_datetime(invoice.quote_created_at),
        invoice.expected_atomic,
        invoice.observed_atomic,
        invoice.xmr_address,
        invoice.xmr_account_index,
        invoice.xmr_address_index,
        invoice.required_confirmations,
        invoice.observed_confirmations,
        invoice.deposit_txid,
        invoice.sweep_txid,
        int(invoice.sweep_required),
        invoice.status.value,
        invoice.status_note,
        _serialize_datetime(invoice.created_at),
        _serialize_datetime(invoice.expires_at),
        _serialize_optional_datetime(invoice.expired_at),
        _serialize_optional_datetime(invoice.settled_at),
        _serialize_datetime(invoice.updated_at),
    )


def _row_to_invoice(row: sqlite3.Row) -> Invoice:
    return Invoice(
        id=row["id"],
        status_token=row["status_token"],
        service_id=row["service_id"],
        service_version=int(row["service_version"]),
        service_name_snapshot=row["service_name_snapshot"],
        service_description_snapshot=row["service_description_snapshot"],
        duration_label_snapshot=row["duration_label_snapshot"],
        category_id=row["category_id"],
        category_name_snapshot=row["category_name_snapshot"],
        category_description_snapshot=row["category_description_snapshot"],
        price_usd_cents=int(row["price_usd_cents"]),
        xmr_usd_rate=row["xmr_usd_rate"],
        rate_source=row["rate_source"],
        quote_created_at=_parse_datetime(row["quote_created_at"]),
        expected_atomic=int(row["expected_atomic"]),
        observed_atomic=int(row["observed_atomic"]),
        xmr_address=row["xmr_address"],
        xmr_account_index=int(row["xmr_account_index"]),
        xmr_address_index=int(row["xmr_address_index"]),
        required_confirmations=int(row["required_confirmations"]),
        observed_confirmations=int(row["observed_confirmations"]),
        deposit_txid=row["deposit_txid"],
        sweep_txid=row["sweep_txid"],
        sweep_required=bool(row["sweep_required"]),
        status=PaymentStatus(row["status"]),
        status_note=row["status_note"],
        created_at=_parse_datetime(row["created_at"]),
        expires_at=_parse_datetime(row["expires_at"]),
        expired_at=_parse_optional_datetime(row["expired_at"]),
        settled_at=_parse_optional_datetime(row["settled_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
    )


def _serialize_datetime(value: datetime) -> str:
    _require_aware_datetime(value, "timestamp")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _serialize_optional_datetime(value: datetime | None) -> str | None:
    return _serialize_datetime(value) if value is not None else None


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    _require_aware_datetime(parsed, "stored timestamp")
    return parsed


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return _parse_datetime(value) if value is not None else None


def _require_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvoiceValidationError(f"{label} must be a positive integer")


def _require_non_negative_int(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvoiceValidationError(f"{label} must be a non-negative integer")


def _require_aware_datetime(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvoiceValidationError(f"{label} must be timezone-aware")


def _require_token(value: str, label: str) -> None:
    if not isinstance(value, str) or not 16 <= len(value) <= 200:
        raise InvoiceValidationError(f"{label} is invalid")


def _validate_admin_password_hash(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 32 <= len(value) <= 1_024
        or not value.startswith(("scrypt:", "pbkdf2:"))
    ):
        raise PersistenceError("admin password hash is invalid")


def _ensure_fulfillment_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(invoices)")
    }
    additions = {
        "fulfillment_status": (
            "ALTER TABLE invoices ADD COLUMN fulfillment_status TEXT NOT NULL "
            "DEFAULT 'unfulfilled' CHECK (fulfillment_status IN ('unfulfilled', 'fulfilled'))"
        ),
        "fulfillment_note": (
            "ALTER TABLE invoices ADD COLUMN fulfillment_note TEXT NOT NULL DEFAULT ''"
        ),
        "fulfilled_at": "ALTER TABLE invoices ADD COLUMN fulfilled_at TEXT",
    }
    for name, statement in additions.items():
        if name not in columns:
            connection.execute(statement)


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL CHECK (published IN (0, 1)),
    archived INTEGER NOT NULL CHECK (archived IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    price_usd_cents INTEGER NOT NULL CHECK (price_usd_cents > 0),
    duration_label TEXT,
    published INTEGER NOT NULL CHECK (published IN (0, 1)),
    archived INTEGER NOT NULL CHECK (archived IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    status_token TEXT NOT NULL UNIQUE,
    service_id TEXT NOT NULL REFERENCES services(id) ON DELETE RESTRICT,
    service_version INTEGER NOT NULL CHECK (service_version > 0),
    service_name_snapshot TEXT NOT NULL,
    service_description_snapshot TEXT NOT NULL,
    duration_label_snapshot TEXT,
    category_id TEXT NOT NULL,
    category_name_snapshot TEXT NOT NULL,
    category_description_snapshot TEXT NOT NULL,
    price_usd_cents INTEGER NOT NULL CHECK (price_usd_cents > 0),
    xmr_usd_rate TEXT NOT NULL,
    rate_source TEXT NOT NULL,
    quote_created_at TEXT NOT NULL,
    expected_atomic INTEGER NOT NULL CHECK (expected_atomic > 0),
    observed_atomic INTEGER NOT NULL DEFAULT 0 CHECK (observed_atomic >= 0),
    xmr_address TEXT NOT NULL UNIQUE,
    xmr_account_index INTEGER NOT NULL CHECK (xmr_account_index >= 0),
    xmr_address_index INTEGER NOT NULL CHECK (xmr_address_index >= 0),
    required_confirmations INTEGER NOT NULL CHECK (required_confirmations > 0),
    observed_confirmations INTEGER NOT NULL DEFAULT 0 CHECK (observed_confirmations >= 0),
    deposit_txid TEXT,
    sweep_txid TEXT,
    sweep_required INTEGER NOT NULL CHECK (sweep_required IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN (
        'awaiting_payment',
        'paid_pending_confirmations',
        'paid_pending_sweep',
        'sweeping_to_cold',
        'settled',
        'expired'
    )),
    status_note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    expired_at TEXT,
    settled_at TEXT,
    fulfillment_status TEXT NOT NULL DEFAULT 'unfulfilled' CHECK (
        fulfillment_status IN ('unfulfilled', 'fulfilled')
    ),
    fulfillment_note TEXT NOT NULL DEFAULT '',
    fulfilled_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (xmr_account_index, xmr_address_index),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK ((status = 'settled') = (settled_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS admin_login_guard (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    window_started_at TEXT NOT NULL,
    failure_count INTEGER NOT NULL CHECK (failure_count >= 0),
    blocked_until TEXT
);

CREATE TABLE IF NOT EXISTS admin_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    credential_version INTEGER NOT NULL CHECK (credential_version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_poll_claims (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
    claim_token TEXT NOT NULL UNIQUE,
    claimed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_sweep_attempts (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(id) ON DELETE CASCADE,
    attempt_token TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    uncertain INTEGER NOT NULL CHECK (uncertain IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_services_public
    ON services(category_id, published, archived, sort_order);
CREATE INDEX IF NOT EXISTS idx_invoices_open
    ON invoices(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_invoices_service
    ON invoices(service_id, created_at);
CREATE INDEX IF NOT EXISTS idx_invoices_admin
    ON invoices(fulfillment_status, status, created_at);
"""
