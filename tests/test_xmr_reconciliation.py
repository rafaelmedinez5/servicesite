from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from app import create_app
from app.catalog import CategoryRecord, ServiceRecord
from app.payments.invoice import InvoiceCreator, PaymentStatus, XmrQuote
from app.payments.xmr_reconciliation import (
    PollSummary,
    ReconciliationConfig,
    XmrReconciliationService,
    XmrReconciliationUnavailable,
)
from app.payments.xmr_wallet_rpc import (
    XmrSubaddress,
    XmrWalletRpcRemoteError,
    XmrWalletRpcTransportError,
)
from app.persistence import SQLiteDatabase, ServicesiteRepository


NOW = datetime(2026, 8, 17, 20, 0, tzinfo=timezone.utc)
COLD_ADDRESS = "test-only-cold-destination"


class CreationWallet:
    def __init__(self):
        self.value = 0

    def create_subaddress(self, label):
        self.value += 1
        return XmrSubaddress(
            address=f"test-only-invoice-address-{self.value}",
            account_index=7,
            address_index=self.value,
        )


class FakeReconciliationWallet:
    def __init__(self, *, incoming=None, outgoing=None, sweep_outcomes=None):
        self.incoming = incoming if incoming is not None else []
        self.outgoing = outgoing if outgoing is not None else []
        self.sweep_outcomes = list(sweep_outcomes or [])
        self.incoming_calls = []
        self.outgoing_calls = []
        self.sweep_calls = []

    def get_transfers_in(self, account_index):
        self.incoming_calls.append(account_index)
        if isinstance(self.incoming, Exception):
            raise self.incoming
        return list(self.incoming)

    def get_transfers_out(self, account_index):
        self.outgoing_calls.append(account_index)
        if isinstance(self.outgoing, Exception):
            raise self.outgoing
        return list(self.outgoing)

    def sweep_all(self, **kwargs):
        self.sweep_calls.append(kwargs)
        outcome = self.sweep_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BlockingSweepWallet(FakeReconciliationWallet):
    def __init__(self, *, incoming):
        super().__init__(
            incoming=incoming,
            sweep_outcomes=[{"tx_hash_list": ["test-only-concurrent-sweep"]}],
        )
        self.started = threading.Event()
        self.release = threading.Event()

    def sweep_all(self, **kwargs):
        self.sweep_calls.append(kwargs)
        self.started.set()
        assert self.release.wait(timeout=5)
        return self.sweep_outcomes.pop(0)


@dataclass
class ReconciliationContext:
    repository: ServicesiteRepository
    creation_wallet: CreationWallet


@pytest.fixture
def reconciliation_context(tmp_path):
    database = SQLiteDatabase(tmp_path / "reconciliation.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    repository.insert_category(
        CategoryRecord(
            id="category-security",
            name="Security Services",
            slug="security-services",
            description="Authorized work.",
            published=True,
            archived=False,
            sort_order=10,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    repository.insert_service(
        ServiceRecord(
            id="service-assessment",
            category_id="category-security",
            name="Security Assessment",
            slug="security-assessment",
            description="Authorized assessment.",
            price_usd_cents=10_000,
            duration_label="One engagement",
            published=True,
            archived=False,
            sort_order=10,
            version=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return ReconciliationContext(repository, CreationWallet())


def _invoice(context, *, sweep_required=False):
    creator = InvoiceCreator(
        context.repository,
        context.creation_wallet,
        required_confirmations=10,
        sweep_required=sweep_required,
        now_factory=lambda: NOW,
    )
    return creator.create_invoice(
        "service-assessment",
        XmrQuote(
            usd_per_xmr=Decimal("200"),
            source="test-only-rate",
            quoted_at=NOW - timedelta(seconds=30),
        ),
    )


def _incoming(invoice, *, amount=None, confirmations=0, txid="test-only-deposit"):
    return {
        "amount": invoice.expected_atomic if amount is None else amount,
        "confirmations": confirmations,
        "txid": txid,
        "height": 100,
        "timestamp": int((NOW - timedelta(minutes=1)).timestamp()),
        "subaddr_index": {
            "major": invoice.xmr_account_index,
            "minor": invoice.xmr_address_index,
        },
    }


def _outgoing(invoice, *, txid="test-only-sweep", timestamp=None):
    return {
        "txid": txid,
        "timestamp": timestamp or int((NOW + timedelta(seconds=30)).timestamp()),
        "subaddr_index": {
            "major": invoice.xmr_account_index,
            "minor": invoice.xmr_address_index,
        },
        "destinations": [{"address": COLD_ADDRESS, "amount": 1}],
    }


def _service(
    context,
    wallet,
    *,
    now=NOW + timedelta(minutes=1),
    sweep_enabled=False,
    reconcile_delay=timedelta(minutes=5),
):
    return XmrReconciliationService(
        context.repository,
        wallet,
        ReconciliationConfig(
            account_index=7,
            sweep_enabled=sweep_enabled,
            cold_address=COLD_ADDRESS if sweep_enabled else "",
            sweep_account_index=7,
            sweep_priority=2,
            sweep_relay=True,
            claim_lease=timedelta(minutes=10),
            uncertain_reconcile_delay=reconcile_delay,
        ),
        now_factory=lambda: now,
    )


def test_no_transfer_remains_awaiting_then_expires_at_boundary(reconciliation_context):
    invoice = _invoice(reconciliation_context)
    wallet = FakeReconciliationWallet()

    first = _service(reconciliation_context, wallet).poll()
    boundary = _service(
        reconciliation_context, wallet, now=invoice.expires_at
    ).poll()

    assert first.processed == 1
    assert reconciliation_context.repository.get_invoice(invoice.id).status is PaymentStatus.EXPIRED
    assert boundary.expired == 1


def test_partial_payment_is_summed_and_does_not_settle(reconciliation_context):
    invoice = _invoice(reconciliation_context)
    wallet = FakeReconciliationWallet(
        incoming=[
            _incoming(
                invoice,
                amount=invoice.expected_atomic // 4,
                confirmations=10,
                txid="test-only-first",
            ),
            _incoming(
                invoice,
                amount=invoice.expected_atomic // 4,
                confirmations=8,
                txid="test-only-second",
            ),
        ]
    )

    summary = _service(reconciliation_context, wallet).poll()
    stored = reconciliation_context.repository.get_invoice(invoice.id)

    assert summary.partial == 1
    assert stored.observed_atomic == invoice.expected_atomic // 2
    assert stored.observed_confirmations == 10
    assert stored.deposit_txid == "test-only-first"
    assert stored.status is PaymentStatus.PAID_PENDING_CONFIRMATIONS


@pytest.mark.parametrize("extra_atomic", [0, 123_456])
def test_exact_and_overpayment_settle_when_sweeping_is_disabled(
    reconciliation_context, extra_atomic
):
    invoice = _invoice(reconciliation_context, sweep_required=False)
    wallet = FakeReconciliationWallet(
        incoming=[
            _incoming(
                invoice,
                amount=invoice.expected_atomic + extra_atomic,
                confirmations=10,
            )
        ]
    )

    summary = _service(reconciliation_context, wallet).poll()
    stored = reconciliation_context.repository.get_invoice(invoice.id)

    assert summary.settled == 1
    assert stored.status is PaymentStatus.SETTLED
    assert stored.observed_atomic == invoice.expected_atomic + extra_atomic
    assert wallet.sweep_calls == []


def test_confirmation_coverage_requires_enough_confirmed_atomic_units(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context)
    wallet = FakeReconciliationWallet(
        incoming=[
            _incoming(
                invoice,
                amount=invoice.expected_atomic // 2,
                confirmations=10,
                txid="test-only-confirmed-half",
            ),
            _incoming(
                invoice,
                amount=invoice.expected_atomic // 2,
                confirmations=2,
                txid="test-only-new-half",
            ),
        ]
    )

    low = _service(reconciliation_context, wallet).poll()
    wallet.incoming[1]["confirmations"] = 10
    sufficient = _service(
        reconciliation_context, wallet, now=NOW + timedelta(minutes=2)
    ).poll()

    assert low.pending_confirmations == 1
    assert sufficient.settled == 1
    assert reconciliation_context.repository.get_invoice(invoice.id).status is PaymentStatus.SETTLED


def test_account_and_subaddress_are_both_required_for_matching(reconciliation_context):
    invoice = _invoice(reconciliation_context)
    wrong_account = _incoming(invoice, confirmations=10)
    wrong_account["subaddr_index"]["major"] = 8
    wallet = FakeReconciliationWallet(incoming=[wrong_account])

    _service(reconciliation_context, wallet).poll()

    stored = reconciliation_context.repository.get_invoice(invoice.id)
    assert stored.observed_atomic == 0
    assert stored.status is PaymentStatus.AWAITING_PAYMENT


def test_rpc_unavailable_leaves_invoice_unchanged(reconciliation_context):
    invoice = _invoice(reconciliation_context)
    wallet = FakeReconciliationWallet(
        incoming=XmrWalletRpcTransportError("test-only outage")
    )

    with pytest.raises(XmrReconciliationUnavailable):
        _service(reconciliation_context, wallet).poll()

    assert reconciliation_context.repository.get_invoice(invoice.id) == invoice


def test_confirmed_sweep_invoice_waits_without_expiring_when_sweep_gate_is_off(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)]
    )

    summary = _service(
        reconciliation_context,
        wallet,
        now=invoice.expires_at + timedelta(minutes=1),
        sweep_enabled=False,
    ).poll()
    stored = reconciliation_context.repository.get_invoice(invoice.id)

    assert summary.pending_sweep == 1
    assert stored.status is PaymentStatus.PAID_PENDING_SWEEP
    assert stored.expired_at is None


def test_sweep_success_targets_only_invoice_subaddress_and_settles(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)],
        sweep_outcomes=[{"tx_hash_list": ["test-only-sweep-success"]}],
    )

    summary = _service(
        reconciliation_context, wallet, sweep_enabled=True
    ).poll()
    stored = reconciliation_context.repository.get_invoice(invoice.id)

    assert summary.settled == 1
    assert stored.status is PaymentStatus.SETTLED
    assert stored.sweep_txid == "test-only-sweep-success"
    assert wallet.sweep_calls == [
        {
            "address": COLD_ADDRESS,
            "account_index": invoice.xmr_account_index,
            "priority": 2,
            "relay": True,
            "subaddr_indices": [invoice.xmr_address_index],
        }
    ]


def test_ordinary_sweep_rejection_returns_to_retryable_state(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)],
        sweep_outcomes=[
            XmrWalletRpcRemoteError(-4),
            {"tx_hash_list": ["test-only-sweep-retry"]},
        ],
    )

    failed = _service(reconciliation_context, wallet, sweep_enabled=True).poll()
    retried = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=2),
        sweep_enabled=True,
    ).poll()

    assert failed.errors == 1
    assert retried.settled == 1
    assert len(wallet.sweep_calls) == 2
    assert reconciliation_context.repository.get_invoice(invoice.id).status is PaymentStatus.SETTLED


def test_reconciliation_log_omits_payment_and_wallet_identifiers(
    reconciliation_context, caplog
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    deposit_txid = "test-only-sensitive-deposit-transaction"
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10, txid=deposit_txid)],
        sweep_outcomes=[XmrWalletRpcRemoteError(-4)],
    )

    with caplog.at_level(logging.WARNING, logger="servicesite.xmr_reconciliation"):
        summary = _service(
            reconciliation_context, wallet, sweep_enabled=True
        ).poll()

    rendered = caplog.text
    assert summary.errors == 1
    assert "invoice_reconciliation_error" in rendered
    assert invoice.id not in rendered
    assert invoice.xmr_address not in rendered
    assert deposit_txid not in rendered
    assert COLD_ADDRESS not in rendered


def test_stored_sweep_txid_settles_without_another_sweep_call(reconciliation_context):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    repository = reconciliation_context.repository
    repository.record_observation(
        invoice.id,
        observed_atomic=invoice.expected_atomic,
        observed_confirmations=10,
        deposit_txid="test-only-deposit",
        now=NOW + timedelta(minutes=1),
    )
    repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        now=NOW + timedelta(minutes=1),
    )
    repository.transition_status(
        invoice.id,
        PaymentStatus.PAID_PENDING_SWEEP,
        now=NOW + timedelta(minutes=1),
    )
    attempt_token = "test-only-attempt-token-12345"
    repository.claim_sweep(
        invoice.id, attempt_token=attempt_token, now=NOW + timedelta(minutes=1)
    )
    repository.record_claimed_sweep_transaction(
        invoice.id,
        attempt_token=attempt_token,
        sweep_txid="test-only-stored-sweep",
        now=NOW + timedelta(minutes=1),
    )
    wallet = FakeReconciliationWallet(incoming=[])

    summary = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=2),
        sweep_enabled=True,
    ).poll()

    assert summary.settled == 1
    assert wallet.sweep_calls == []
    assert repository.get_invoice(invoice.id).status is PaymentStatus.SETTLED


def test_response_loss_reconciles_outgoing_history_without_duplicate_sweep(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)],
        sweep_outcomes=[XmrWalletRpcTransportError("response lost")],
    )

    uncertain = _service(
        reconciliation_context, wallet, sweep_enabled=True
    ).poll()
    wallet.outgoing = [_outgoing(invoice)]
    reconciled = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=2),
        sweep_enabled=True,
    ).poll()

    assert uncertain.errors == 1
    assert reconciled.reconciled_sweeps == 1
    assert reconciled.settled == 1
    assert len(wallet.sweep_calls) == 1
    stored = reconciliation_context.repository.get_invoice(invoice.id)
    assert stored.status is PaymentStatus.SETTLED
    assert stored.sweep_txid == "test-only-sweep"


def test_malformed_sweep_result_remains_uncertain_without_duplicate_sweep(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)],
        outgoing=[_outgoing(invoice, txid="test-only-recovered-sweep")],
        sweep_outcomes=[{"tx_hash_list": [""]}],
    )

    malformed = _service(
        reconciliation_context, wallet, sweep_enabled=True
    ).poll()
    reconciled = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=2),
        sweep_enabled=True,
    ).poll()

    assert malformed.errors == 1
    assert reconciled.reconciled_sweeps == 1
    assert len(wallet.sweep_calls) == 1
    stored = reconciliation_context.repository.get_invoice(invoice.id)
    assert stored.status is PaymentStatus.SETTLED
    assert stored.sweep_txid == "test-only-recovered-sweep"


def test_uncertain_attempt_is_released_only_after_reconciliation_delay_then_retried(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = FakeReconciliationWallet(
        incoming=[_incoming(invoice, confirmations=10)],
        sweep_outcomes=[
            XmrWalletRpcTransportError("response lost"),
            {"tx_hash_list": ["test-only-eventual-sweep"]},
        ],
    )

    _service(reconciliation_context, wallet, sweep_enabled=True).poll()
    waiting = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=4),
        sweep_enabled=True,
    ).poll()
    released = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=6),
        sweep_enabled=True,
    ).poll()
    retried = _service(
        reconciliation_context,
        wallet,
        now=NOW + timedelta(minutes=7),
        sweep_enabled=True,
    ).poll()

    assert waiting.sweeping == 1
    assert released.pending_sweep == 1
    assert retried.settled == 1
    assert len(wallet.sweep_calls) == 2


def test_overlapping_poll_attempt_is_skipped_while_first_sweep_holds_db_claim(
    reconciliation_context,
):
    invoice = _invoice(reconciliation_context, sweep_required=True)
    wallet = BlockingSweepWallet(
        incoming=[_incoming(invoice, confirmations=10)]
    )
    service = _service(reconciliation_context, wallet, sweep_enabled=True)
    result = {}

    thread = threading.Thread(target=lambda: result.setdefault("first", service.poll()))
    thread.start()
    assert wallet.started.wait(timeout=5)
    second = service.poll()
    wallet.release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert second.skipped_locked == 1
    assert result["first"].settled == 1
    assert len(wallet.sweep_calls) == 1


class FakeEndpointService:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def poll(self):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def endpoint_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret")
    database = SQLiteDatabase(tmp_path / "endpoint.db")
    database.initialize()
    repository = ServicesiteRepository(database)
    service = FakeEndpointService(PollSummary(open_invoices=1, processed=1))
    app = create_app(
        {
            "TESTING": True,
            "DB_PATH": str(tmp_path / "endpoint.db"),
            "SERVICESITE_REPOSITORY": repository,
            "SERVICESITE_RECONCILIATION_SERVICE": service,
            "X_INTERNAL_TOKEN": "t" * 32,
        }
    )
    return app, service


def test_internal_endpoint_requires_loopback_and_exact_token(endpoint_app):
    app, service = endpoint_app
    client = app.test_client()

    assert client.post("/internal/poll-xmr").status_code == 403
    assert client.post(
        "/internal/poll-xmr", headers={"X-Internal-Token": "wrong"}
    ).status_code == 403
    assert client.post(
        "/internal/poll-xmr",
        headers={"X-Internal-Token": "t" * 32},
        environ_base={"REMOTE_ADDR": "203.0.113.5"},
    ).status_code == 403
    allowed = client.post(
        "/internal/poll-xmr",
        headers={
            "X-Internal-Token": "t" * 32,
            "X-Forwarded-For": "203.0.113.5",
        },
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert allowed.status_code == 200
    assert allowed.get_json()["processed"] == 1
    assert service.calls == 1
    assert allowed.headers["Cache-Control"] == "no-store, private, max-age=0"
    assert allowed.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"


def test_internal_endpoint_returns_sanitized_failure(endpoint_app):
    app, service = endpoint_app
    service.outcome = XmrReconciliationUnavailable("test-only sensitive detail")

    response = app.test_client().post(
        "/internal/poll-xmr",
        headers={"X-Internal-Token": "t" * 32},
        environ_base={"REMOTE_ADDR": "::1"},
    )

    assert response.status_code == 503
    assert response.get_json() == {"ok": False, "error": "wallet_unavailable"}
    assert "sensitive" not in response.get_data(as_text=True)
