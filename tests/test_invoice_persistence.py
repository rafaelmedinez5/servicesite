from __future__ import annotations

import inspect
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.catalog import CategoryRecord, ServiceRecord
from app.payments.invoice import (
    IllegalInvoiceTransition,
    InvoiceCreator,
    InvoiceValidationError,
    PaymentStatus,
    ServiceUnavailableError,
    XmrQuote,
    calculate_expected_atomic,
)
from app.payments.xmr_wallet_rpc import XmrSubaddress
from app.persistence import (
    CatalogChangedError,
    DatabaseNotFreshError,
    FulfillmentStatus,
    InvoicePersistenceError,
    SQLiteDatabase,
    ServicesiteRepository,
)


NOW = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)


class FakeWallet:
    def __init__(self, *, duplicate=False, failure=None):
        self.duplicate = duplicate
        self.failure = failure
        self.calls = []

    def create_subaddress(self, label):
        self.calls.append(label)
        if self.failure:
            raise self.failure
        address_index = 1 if self.duplicate else len(self.calls)
        address = "test-only-subaddress-1" if self.duplicate else f"test-only-subaddress-{address_index}"
        return XmrSubaddress(
            address=address,
            account_index=7,
            address_index=address_index,
        )


class ValueSequence:
    def __init__(self):
        self.value = 0

    def invoice_id(self):
        self.value += 1
        return f"{self.value:032x}"

    def token(self):
        return f"test-token-{self.value:04d}-" + ("x" * 32)


@dataclass
class RepositoryContext:
    database: SQLiteDatabase
    repository: ServicesiteRepository


@pytest.fixture
def repository_context(tmp_path):
    database = SQLiteDatabase(tmp_path / "servicesite.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.insert_category(_category())
    repository.insert_service(_service())
    return RepositoryContext(database=database, repository=repository)


def _category(*, published=True, archived=False, category_id="category-security"):
    return CategoryRecord(
        id=category_id,
        name="Security Services",
        slug=f"{category_id}-slug",
        description="Authorized security work.",
        published=published,
        archived=archived,
        sort_order=10,
        created_at=NOW,
        updated_at=NOW,
    )


def _service(
    *,
    published=True,
    archived=False,
    service_id="service-assessment",
    category_id="category-security",
    slug="security-assessment",
    price_usd_cents=10_000,
):
    return ServiceRecord(
        id=service_id,
        category_id=category_id,
        name="Security Assessment",
        slug=slug,
        description="Authorized assessment with a written report.",
        price_usd_cents=price_usd_cents,
        duration_label="One engagement",
        published=published,
        archived=archived,
        sort_order=10,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _quote(rate="200"):
    return XmrQuote.from_string(
        rate,
        source="test-only-rate-source",
        quoted_at=NOW - timedelta(seconds=30),
    )


def _creator(repository, wallet, *, sweep_required=False, sequence=None, now=NOW):
    sequence = sequence or ValueSequence()
    return InvoiceCreator(
        repository,
        wallet,
        required_confirmations=10,
        sweep_required=sweep_required,
        invoice_ttl=timedelta(hours=2),
        now_factory=lambda: now,
        invoice_id_factory=sequence.invoice_id,
        status_token_factory=sequence.token,
    )


def test_fresh_schema_initialization_is_idempotent_and_uses_wal(tmp_path):
    database_path = tmp_path / "fresh.db"
    database = SQLiteDatabase(database_path)

    database.initialize()
    database.initialize()

    connection = database.connect()
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert tables == {
        "schema_meta",
        "categories",
        "services",
        "invoices",
        "invoice_poll_claims",
        "invoice_sweep_attempts",
        "admin_login_guard",
        "admin_credentials",
        "customer_accounts",
        "customer_login_guard",
    }
    assert journal_mode.lower() == "wal"
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600


def test_schema_initialization_refuses_an_unrecognized_existing_database(tmp_path):
    database_path = tmp_path / "not-fresh.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_data(id INTEGER PRIMARY KEY)")

    with pytest.raises(DatabaseNotFreshError, match="import is refused"):
        SQLiteDatabase(database_path).initialize()


def test_schema_version_one_is_upgraded_with_reconciliation_tables(tmp_path):
    database = SQLiteDatabase(tmp_path / "upgrade.db")
    database.initialize()
    connection = database.connect()
    try:
        connection.execute("DROP TABLE invoice_poll_claims")
        connection.execute("DROP TABLE invoice_sweep_attempts")
        connection.execute(
            "UPDATE schema_meta SET value='1' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()
    connection = database.connect()
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert version == "5"
    assert {"invoice_poll_claims", "invoice_sweep_attempts"} <= tables


def test_schema_version_two_is_upgraded_with_fulfillment_columns(tmp_path):
    database = SQLiteDatabase(tmp_path / "upgrade-v2.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.insert_category(_category())
    repository.insert_service(_service())
    invoice = _creator(repository, FakeWallet()).create_invoice(
        "service-assessment", _quote()
    )
    connection = database.connect()
    try:
        connection.execute("DROP TABLE admin_login_guard")
        connection.execute("DROP TABLE admin_credentials")
        connection.execute("DROP INDEX idx_invoices_admin")
        connection.execute("ALTER TABLE invoices DROP COLUMN fulfilled_at")
        connection.execute("ALTER TABLE invoices DROP COLUMN fulfillment_note")
        connection.execute("ALTER TABLE invoices DROP COLUMN fulfillment_status")
        connection.execute(
            "UPDATE schema_meta SET value='2' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()
    connection = database.connect()
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(invoices)")
        }
        guard_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_login_guard'"
        ).fetchone()
    finally:
        connection.close()

    assert version == "5"
    assert {"fulfillment_status", "fulfillment_note", "fulfilled_at"} <= columns
    assert guard_exists is not None
    assert repository.get_invoice(invoice.id) == invoice
    assert (
        repository.get_admin_purchase(invoice.id).fulfillment_status
        is FulfillmentStatus.UNFULFILLED
    )


def test_schema_version_three_is_upgraded_with_admin_credentials(tmp_path):
    database = SQLiteDatabase(tmp_path / "upgrade-v3.db")
    database.initialize()
    connection = database.connect()
    try:
        connection.execute("DROP TABLE admin_credentials")
        connection.execute(
            "UPDATE schema_meta SET value='3' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()
    repository = ServicesiteRepository(database)
    connection = database.connect()
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        credential_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_credentials'"
        ).fetchone()
    finally:
        connection.close()

    assert version == "5"
    assert credential_table is not None
    assert repository.get_admin_credential() is None


def test_schema_version_four_is_upgraded_with_customer_accounts(tmp_path):
    database = SQLiteDatabase(tmp_path / "upgrade-v4.db")
    database.initialize()
    connection = database.connect()
    try:
        connection.execute("DROP TABLE customer_login_guard")
        connection.execute("DROP TABLE customer_accounts")
        connection.execute(
            "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    database.initialize()
    connection = database.connect()
    try:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    assert version == "5"
    assert {"customer_accounts", "customer_login_guard"} <= tables


def test_each_invoice_gets_a_unique_persisted_subaddress(repository_context):
    wallet = FakeWallet()
    sequence = ValueSequence()
    creator = _creator(repository_context.repository, wallet, sequence=sequence)

    first = creator.create_invoice("service-assessment", _quote())
    second = creator.create_invoice("service-assessment", _quote())

    assert first.xmr_address != second.xmr_address
    assert first.xmr_address_index != second.xmr_address_index
    assert first.xmr_account_index == second.xmr_account_index == 7
    assert wallet.calls == [f"invoice:{first.id}", f"invoice:{second.id}"]
    assert repository_context.repository.get_invoice(first.id) == first
    assert repository_context.repository.get_invoice(second.id) == second


def test_invoice_locks_price_rate_amount_confirmations_and_catalog_snapshots(
    repository_context,
):
    sequence = ValueSequence()
    wallet = FakeWallet()
    creator = _creator(
        repository_context.repository, wallet, sequence=sequence
    )

    invoice = creator.create_invoice("service-assessment", _quote("200"))
    repository_context.repository.update_service_price(
        "service-assessment", price_usd_cents=20_000, now=NOW + timedelta(minutes=1)
    )
    stored = repository_context.repository.get_invoice(invoice.id)

    assert stored.price_usd_cents == 10_000
    assert stored.xmr_usd_rate == "200"
    assert stored.expected_atomic == 500_000_000_000
    assert stored.required_confirmations == 10
    assert stored.service_version == 1
    assert stored.service_name_snapshot == "Security Assessment"
    assert stored.category_name_snapshot == "Security Services"

    later_creator = _creator(
        repository_context.repository,
        wallet,
        sequence=sequence,
        now=NOW + timedelta(minutes=2),
    )
    later = later_creator.create_invoice("service-assessment", _quote("200"))
    assert later.price_usd_cents == 20_000
    assert later.expected_atomic == 1_000_000_000_000
    assert later.service_version == 2


def test_atomic_calculation_rounds_up_without_binary_floats():
    assert calculate_expected_atomic(100, Decimal("3")) == 333_333_333_334
    with pytest.raises(InvoiceValidationError):
        calculate_expected_atomic(100, 3.0)


def test_database_failure_rolls_back_the_entire_invoice_insert(repository_context):
    connection = repository_context.database.connect()
    try:
        connection.execute(
            """
            CREATE TRIGGER reject_test_invoice
            AFTER INSERT ON invoices
            BEGIN
                SELECT RAISE(ABORT, 'forced test rollback');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    creator = _creator(repository_context.repository, FakeWallet())
    with pytest.raises(InvoicePersistenceError):
        creator.create_invoice("service-assessment", _quote())

    assert repository_context.repository.count_invoices() == 0


def test_wallet_failure_leaves_no_invoice_row(repository_context):
    wallet = FakeWallet(failure=RuntimeError("test-only wallet outage"))
    creator = _creator(repository_context.repository, wallet)

    with pytest.raises(RuntimeError, match="wallet outage"):
        creator.create_invoice("service-assessment", _quote())

    assert repository_context.repository.count_invoices() == 0


def test_duplicate_wallet_subaddress_is_rejected_by_database_constraints(
    repository_context,
):
    wallet = FakeWallet(duplicate=True)
    creator = _creator(
        repository_context.repository, wallet, sequence=ValueSequence()
    )

    creator.create_invoice("service-assessment", _quote())
    with pytest.raises(InvoicePersistenceError):
        creator.create_invoice("service-assessment", _quote())

    assert repository_context.repository.count_invoices() == 1


def test_unpublished_service_is_rejected_before_wallet_access(repository_context):
    repository_context.repository.insert_service(
        _service(
            service_id="service-hidden",
            slug="hidden-service",
            published=False,
        )
    )
    wallet = FakeWallet()
    creator = _creator(repository_context.repository, wallet)

    with pytest.raises(ServiceUnavailableError):
        creator.create_invoice("service-hidden", _quote())

    assert wallet.calls == []
    assert repository_context.repository.count_invoices() == 0


def test_invalid_generated_identity_is_rejected_before_wallet_access(
    repository_context,
):
    wallet = FakeWallet()
    creator = InvoiceCreator(
        repository_context.repository,
        wallet,
        required_confirmations=10,
        sweep_required=False,
        now_factory=lambda: NOW,
        invoice_id_factory=lambda: "not-a-valid-id",
        status_token_factory=lambda: "x" * 40,
    )

    with pytest.raises(InvoiceValidationError, match="generated invoice ID"):
        creator.create_invoice("service-assessment", _quote())
    assert wallet.calls == []


def test_catalog_change_between_validation_and_insert_aborts_invoice(
    repository_context,
):
    class ChangingRepository:
        def get_purchasable_service(self, service_id):
            return repository_context.repository.get_purchasable_service(service_id)

        def insert_invoice(self, invoice, expected_service):
            repository_context.repository.update_service_price(
                expected_service.service_id,
                price_usd_cents=12_000,
                now=NOW + timedelta(seconds=1),
            )
            repository_context.repository.insert_invoice(invoice, expected_service)

    creator = _creator(ChangingRepository(), FakeWallet())

    with pytest.raises(CatalogChangedError):
        creator.create_invoice("service-assessment", _quote())
    assert repository_context.repository.count_invoices() == 0


def test_sweep_disabled_invoice_follows_confirmations_to_settlement(
    repository_context,
):
    invoice = _creator(
        repository_context.repository, FakeWallet(), sweep_required=False
    ).create_invoice("service-assessment", _quote())
    observed = repository_context.repository.record_observation(
        invoice.id,
        observed_atomic=invoice.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-deposit",
        now=NOW + timedelta(minutes=1),
    )
    assert observed.fully_paid_and_confirmed

    pending = repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    settled = repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.SETTLED,
        now=NOW + timedelta(minutes=2),
    )

    assert pending.status is PaymentStatus.PAID_PENDING_CONFIRMATIONS
    assert settled.status is PaymentStatus.SETTLED
    assert settled.settled_at == NOW + timedelta(minutes=2)


def test_sweep_required_invoice_follows_all_sweep_states(repository_context):
    invoice = _creator(
        repository_context.repository, FakeWallet(), sweep_required=True
    ).create_invoice("service-assessment", _quote())
    repository_context.repository.record_observation(
        invoice.id,
        observed_atomic=invoice.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-deposit",
        now=NOW + timedelta(minutes=1),
    )
    repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_SWEEP,
        now=NOW + timedelta(minutes=2),
    )
    sweeping = repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.SWEEPING_TO_COLD,
        now=NOW + timedelta(minutes=3),
    )

    retryable = repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_SWEEP,
        now=NOW + timedelta(minutes=4),
    )
    repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.SWEEPING_TO_COLD,
        now=NOW + timedelta(minutes=5),
    )
    repository_context.repository.record_sweep_transaction(
        invoice.id,
        sweep_txid="test-only-sweep",
        now=NOW + timedelta(minutes=6),
    )
    settled = repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.SETTLED,
        now=NOW + timedelta(minutes=7),
    )

    assert sweeping.status is PaymentStatus.SWEEPING_TO_COLD
    assert retryable.status is PaymentStatus.PAID_PENDING_SWEEP
    assert settled.status is PaymentStatus.SETTLED


def test_illegal_state_changes_do_not_modify_the_invoice(repository_context):
    invoice = _creator(repository_context.repository, FakeWallet()).create_invoice(
        "service-assessment", _quote()
    )

    with pytest.raises(IllegalInvoiceTransition):
        repository_context.repository.transition_status(
            invoice.id,
            PaymentStatus.SETTLED,
            now=NOW + timedelta(minutes=1),
        )

    unchanged = repository_context.repository.get_invoice(invoice.id)
    assert unchanged.status is PaymentStatus.AWAITING_PAYMENT
    assert unchanged.updated_at == NOW


def test_partial_payment_cannot_settle(repository_context):
    invoice = _creator(repository_context.repository, FakeWallet()).create_invoice(
        "service-assessment", _quote()
    )
    repository_context.repository.record_observation(
        invoice.id,
        observed_atomic=invoice.expected_atomic - 1,
        observed_confirmations=10,
        deposit_txid="test-only-partial",
        now=NOW + timedelta(minutes=1),
    )
    repository_context.repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(IllegalInvoiceTransition, match="not fully paid"):
        repository_context.repository.transition_status(
            invoice.id,
            PaymentStatus.SETTLED,
            now=NOW + timedelta(minutes=2),
        )


def test_expiry_boundary_is_inclusive_and_only_preconfirmation_states_expire(
    repository_context,
):
    wallet = FakeWallet()
    sequence = ValueSequence()
    invoice = _creator(
        repository_context.repository, wallet, sequence=sequence
    ).create_invoice("service-assessment", _quote())
    with pytest.raises(IllegalInvoiceTransition, match="before its expiry"):
        repository_context.repository.transition_status(
            invoice.id,
            PaymentStatus.EXPIRED,
            now=invoice.expires_at - timedelta(microseconds=1),
        )

    expired = repository_context.repository.transition_status(
        invoice.id, PaymentStatus.EXPIRED, now=invoice.expires_at
    )
    assert expired.status is PaymentStatus.EXPIRED
    assert expired.expired_at == invoice.expires_at

    sweep_invoice = _creator(
        repository_context.repository,
        wallet,
        sweep_required=True,
        sequence=sequence,
        now=NOW + timedelta(hours=3),
    ).create_invoice(
        "service-assessment",
        XmrQuote.from_string(
            "200",
            source="test-only-rate-source",
            quoted_at=NOW + timedelta(hours=2, minutes=59),
        ),
    )
    repository_context.repository.record_observation(
        sweep_invoice.id,
        observed_atomic=sweep_invoice.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-deposit-two",
        now=sweep_invoice.created_at + timedelta(minutes=1),
    )
    repository_context.repository.transition_status(
        sweep_invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=sweep_invoice.created_at + timedelta(minutes=1),
    )
    repository_context.repository.transition_status(
        sweep_invoice.id,
        PaymentStatus.PAID_PENDING_SWEEP,
        now=sweep_invoice.created_at + timedelta(minutes=2),
    )

    with pytest.raises(IllegalInvoiceTransition):
        repository_context.repository.transition_status(
            sweep_invoice.id,
            PaymentStatus.EXPIRED,
            now=sweep_invoice.expires_at,
        )


def test_invoice_repr_hides_bearer_token_address_and_transaction_ids(
    repository_context,
):
    invoice = _creator(repository_context.repository, FakeWallet()).create_invoice(
        "service-assessment", _quote()
    )
    rendered = repr(invoice)
    assert invoice.status_token not in rendered
    assert invoice.xmr_address not in rendered


def test_status_token_lookup_requires_an_exact_token(repository_context):
    invoice = _creator(repository_context.repository, FakeWallet()).create_invoice(
        "service-assessment", _quote()
    )
    assert repository_context.repository.get_invoice_by_token(
        invoice.id, invoice.status_token
    ) == invoice
    assert repository_context.repository.get_invoice_by_token(
        invoice.id, "wrong-token-value"
    ) is None


def test_application_has_exactly_one_callable_invoice_creation_path():
    app_root = Path(inspect.getfile(InvoiceCreator)).parents[1]
    definitions = []
    for path in app_root.rglob("*.py"):
        for line in path.read_text().splitlines():
            if line.lstrip().startswith("def create_invoice("):
                definitions.append(path.relative_to(app_root))

    assert definitions == [Path("payments/invoice.py")]
