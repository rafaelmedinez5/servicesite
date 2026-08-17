from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from app.payments.invoice import Invoice, PaymentStatus
from app.payments.xmr_wallet_rpc import (
    MAX_ATOMIC_UNITS,
    XmrWalletRpcError,
    XmrWalletRpcRemoteError,
)
from app.persistence import ServicesiteRepository, SweepAttempt


class XmrReconciliationError(RuntimeError):
    """A sanitized reconciliation failure requiring a later retry."""


class XmrReconciliationUnavailable(XmrReconciliationError):
    """Wallet history could not be obtained safely for this polling run."""


class XmrTransferDataError(XmrReconciliationError):
    """wallet-RPC returned transfer data that cannot be reconciled safely."""


class XmrSweepUncertainError(XmrReconciliationError):
    """A sweep may have been broadcast but does not yet have a stored txid."""


class ReconciliationWallet(Protocol):
    def get_transfers_in(self, account_index: int) -> list[dict[str, Any]]: ...

    def get_transfers_out(self, account_index: int) -> list[dict[str, Any]]: ...

    def sweep_all(
        self,
        *,
        address: str,
        account_index: int,
        priority: int,
        relay: bool,
        subaddr_indices: list[int],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ReconciliationConfig:
    account_index: int
    sweep_enabled: bool
    cold_address: str = field(repr=False)
    sweep_account_index: int
    sweep_priority: int
    sweep_relay: bool
    claim_lease: timedelta = timedelta(minutes=10)
    uncertain_reconcile_delay: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        for value, label in (
            (self.account_index, "deposit account index"),
            (self.sweep_account_index, "sweep account index"),
            (self.sweep_priority, "sweep priority"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        if self.sweep_priority > 4:
            raise ValueError("sweep priority must not exceed 4")
        if not isinstance(self.sweep_enabled, bool) or not isinstance(self.sweep_relay, bool):
            raise ValueError("sweep flags must be boolean")
        if self.sweep_enabled and (
            not isinstance(self.cold_address, str) or not self.cold_address.strip()
        ):
            raise ValueError("cold destination is required when sweeping is enabled")
        if not isinstance(self.claim_lease, timedelta) or self.claim_lease <= timedelta(0):
            raise ValueError("poll claim lease must be positive")
        if (
            not isinstance(self.uncertain_reconcile_delay, timedelta)
            or self.uncertain_reconcile_delay < timedelta(0)
        ):
            raise ValueError("uncertain sweep reconciliation delay cannot be negative")


@dataclass(frozen=True)
class IncomingTransfer:
    amount_atomic: int
    confirmations: int
    txid: str = field(repr=False)
    account_index: int
    address_index: int
    height: int
    timestamp: int


@dataclass
class PollSummary:
    open_invoices: int = 0
    processed: int = 0
    skipped_locked: int = 0
    expired: int = 0
    partial: int = 0
    pending_confirmations: int = 0
    pending_sweep: int = 0
    sweeping: int = 0
    settled: int = 0
    reconciled_sweeps: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "ok": self.errors == 0,
            "open_invoices": self.open_invoices,
            "processed": self.processed,
            "skipped_locked": self.skipped_locked,
            "expired": self.expired,
            "partial": self.partial,
            "pending_confirmations": self.pending_confirmations,
            "pending_sweep": self.pending_sweep,
            "sweeping": self.sweeping,
            "settled": self.settled,
            "reconciled_sweeps": self.reconciled_sweeps,
            "errors": self.errors,
        }


class XmrReconciliationService:
    """Reconcile stored invoices against wallet history without route logic."""

    def __init__(
        self,
        repository: ServicesiteRepository,
        wallet: ReconciliationWallet,
        config: ReconciliationConfig,
        *,
        now_factory: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        logger: logging.Logger | None = None,
    ) -> None:
        self.repository = repository
        self.wallet = wallet
        self.config = config
        self.now_factory = now_factory
        self.token_factory = token_factory
        self.logger = logger or logging.getLogger("servicesite.xmr_reconciliation")

    def poll(self) -> PollSummary:
        now = self.now_factory()
        _require_aware_datetime(now)
        invoices = self.repository.list_open_invoices()
        summary = PollSummary(open_invoices=len(invoices))
        if not invoices:
            return summary

        try:
            raw_transfers = self.wallet.get_transfers_in(self.config.account_index)
            transfers = [_parse_incoming_transfer(item) for item in raw_transfers]
        except (XmrWalletRpcError, XmrTransferDataError) as exc:
            raise XmrReconciliationUnavailable(
                "incoming wallet history is unavailable"
            ) from exc

        for listed_invoice in invoices:
            claim_token = self.token_factory()
            try:
                claimed = self.repository.claim_invoice(
                    listed_invoice.id,
                    claim_token=claim_token,
                    claimed_at=now,
                    expires_at=now + self.config.claim_lease,
                )
            except Exception:
                summary.errors += 1
                self._log("poll_claim_error", listed_invoice)
                continue
            if not claimed:
                summary.skipped_locked += 1
                continue

            try:
                current = self.repository.get_invoice(listed_invoice.id)
                if current is None or current.status in {
                    PaymentStatus.SETTLED,
                    PaymentStatus.EXPIRED,
                }:
                    continue
                self._process_invoice(current, transfers, now, summary)
                summary.processed += 1
            except Exception:
                summary.errors += 1
                self._log("invoice_reconciliation_error", listed_invoice)
            finally:
                try:
                    self.repository.release_invoice_claim(
                        listed_invoice.id, claim_token=claim_token
                    )
                except Exception:
                    summary.errors += 1
                    self._log("poll_claim_release_error", listed_invoice)

        return summary

    def _process_invoice(
        self,
        invoice: Invoice,
        transfers: list[IncomingTransfer],
        now: datetime,
        summary: PollSummary,
    ) -> None:
        if invoice.xmr_account_index != self.config.account_index:
            raise XmrReconciliationError("invoice account does not match poll account")

        if invoice.status is PaymentStatus.SWEEPING_TO_COLD:
            self._reconcile_sweeping_invoice(invoice, now, summary)
            return

        if invoice.sweep_txid:
            if invoice.status is PaymentStatus.PAID_PENDING_SWEEP:
                invoice = self.repository.transition_status(
                    invoice.id, PaymentStatus.SWEEPING_TO_COLD, now=now
                )
            if invoice.status is PaymentStatus.SWEEPING_TO_COLD:
                self.repository.transition_status(
                    invoice.id, PaymentStatus.SETTLED, now=now
                )
                summary.settled += 1
                return

        observation = _aggregate_invoice_transfers(invoice, transfers)
        observed_atomic = observation.amount_atomic
        observed_confirmations = observation.confirmations
        if invoice.status in {
            PaymentStatus.PAID_PENDING_SWEEP,
            PaymentStatus.SWEEPING_TO_COLD,
        }:
            observed_atomic = max(observed_atomic, invoice.observed_atomic)
            observed_confirmations = max(
                observed_confirmations, invoice.observed_confirmations
            )
        invoice = self.repository.record_observation(
            invoice.id,
            observed_atomic=observed_atomic,
            observed_confirmations=observed_confirmations,
            deposit_txid=observation.deposit_txid,
            now=now,
        )

        if invoice.status in {
            PaymentStatus.AWAITING_PAYMENT,
            PaymentStatus.PAID_PENDING_CONFIRMATIONS,
        } and now >= invoice.expires_at and not invoice.fully_paid_and_confirmed:
            self.repository.transition_status(
                invoice.id, PaymentStatus.EXPIRED, now=now
            )
            summary.expired += 1
            return

        if invoice.observed_atomic <= 0:
            return
        if invoice.status is PaymentStatus.AWAITING_PAYMENT:
            invoice = self.repository.transition_status(
                invoice.id,
                PaymentStatus.PAID_PENDING_CONFIRMATIONS,
                now=now,
            )

        if invoice.observed_atomic < invoice.expected_atomic:
            summary.partial += 1
            return
        if invoice.observed_confirmations < invoice.required_confirmations:
            summary.pending_confirmations += 1
            return

        if not invoice.sweep_required:
            self.repository.transition_status(
                invoice.id, PaymentStatus.SETTLED, now=now
            )
            summary.settled += 1
            return

        if invoice.status is PaymentStatus.PAID_PENDING_CONFIRMATIONS:
            invoice = self.repository.transition_status(
                invoice.id, PaymentStatus.PAID_PENDING_SWEEP, now=now
            )
        if not self.config.sweep_enabled:
            summary.pending_sweep += 1
            return
        self._start_sweep(invoice, now, summary)

    def _start_sweep(
        self, invoice: Invoice, now: datetime, summary: PollSummary
    ) -> None:
        if invoice.xmr_account_index != self.config.sweep_account_index:
            raise XmrReconciliationError("invoice account does not match sweep account")
        attempt_token = self.token_factory()
        claimed = self.repository.claim_sweep(
            invoice.id, attempt_token=attempt_token, now=now
        )
        if claimed is None:
            summary.sweeping += 1
            return

        try:
            result = self.wallet.sweep_all(
                address=self.config.cold_address,
                account_index=invoice.xmr_account_index,
                priority=self.config.sweep_priority,
                relay=self.config.sweep_relay,
                subaddr_indices=[invoice.xmr_address_index],
            )
        except XmrWalletRpcRemoteError:
            self.repository.release_sweep_attempt(
                invoice.id, attempt_token=attempt_token, now=now
            )
            summary.pending_sweep += 1
            raise XmrReconciliationError("wallet rejected sweep request")
        except XmrWalletRpcError as exc:
            self.repository.mark_sweep_uncertain(
                invoice.id, attempt_token=attempt_token, now=now
            )
            summary.sweeping += 1
            raise XmrSweepUncertainError("sweep response is uncertain") from exc

        try:
            sweep_txid = _single_sweep_txid(result)
        except XmrSweepUncertainError:
            self.repository.mark_sweep_uncertain(
                invoice.id, attempt_token=attempt_token, now=now
            )
            summary.sweeping += 1
            raise

        self.repository.record_claimed_sweep_transaction(
            invoice.id,
            attempt_token=attempt_token,
            sweep_txid=sweep_txid,
            now=now,
        )
        self.repository.transition_status(
            invoice.id, PaymentStatus.SETTLED, now=now
        )
        summary.settled += 1

    def _reconcile_sweeping_invoice(
        self, invoice: Invoice, now: datetime, summary: PollSummary
    ) -> None:
        if invoice.sweep_txid:
            self.repository.transition_status(
                invoice.id, PaymentStatus.SETTLED, now=now
            )
            summary.settled += 1
            return
        attempt = self.repository.get_sweep_attempt(invoice.id)
        if attempt is None:
            summary.sweeping += 1
            raise XmrSweepUncertainError("sweep attempt metadata is missing")
        if not self.config.cold_address:
            summary.sweeping += 1
            raise XmrSweepUncertainError(
                "sweep destination is unavailable for reconciliation"
            )

        try:
            outgoing = self.wallet.get_transfers_out(invoice.xmr_account_index)
        except XmrWalletRpcError as exc:
            summary.sweeping += 1
            raise XmrSweepUncertainError(
                "outgoing wallet history is unavailable"
            ) from exc
        matching_txids = _matching_outgoing_txids(
            outgoing,
            invoice=invoice,
            attempt=attempt,
            cold_address=self.config.cold_address,
        )
        if len(matching_txids) > 1:
            summary.sweeping += 1
            raise XmrSweepUncertainError("multiple outgoing sweep candidates exist")
        if len(matching_txids) == 1:
            self.repository.record_claimed_sweep_transaction(
                invoice.id,
                attempt_token=attempt.attempt_token,
                sweep_txid=matching_txids[0],
                now=now,
            )
            self.repository.transition_status(
                invoice.id, PaymentStatus.SETTLED, now=now
            )
            summary.reconciled_sweeps += 1
            summary.settled += 1
            return

        if now - attempt.started_at < self.config.uncertain_reconcile_delay:
            summary.sweeping += 1
            return
        self.repository.release_sweep_attempt(
            invoice.id,
            attempt_token=attempt.attempt_token,
            now=now,
        )
        summary.pending_sweep += 1

    def _log(self, event: str, invoice: Invoice) -> None:
        self.logger.warning(
            "xmr_reconciliation event=%s invoice_ref=%s",
            event,
            invoice.id[:8],
        )


@dataclass(frozen=True)
class InvoiceObservation:
    amount_atomic: int
    confirmations: int
    deposit_txid: str | None = field(repr=False)


def _parse_incoming_transfer(item: dict[str, Any]) -> IncomingTransfer:
    if not isinstance(item, dict):
        raise XmrTransferDataError("incoming transfer is not an object")
    subaddress = _parse_subaddress_index(item.get("subaddr_index"))
    if subaddress is None:
        raise XmrTransferDataError("incoming transfer has no subaddress index")
    amount = _non_negative_int(item.get("amount"), "incoming amount")
    confirmations = _non_negative_int(
        item.get("confirmations"), "incoming confirmations"
    )
    txid = _nonempty_text(item.get("txid"), "incoming transaction ID")
    height = _optional_non_negative_int(item.get("height", 0), "incoming height")
    timestamp = _optional_non_negative_int(
        item.get("timestamp", 0), "incoming timestamp"
    )
    if amount > MAX_ATOMIC_UNITS:
        raise XmrTransferDataError("incoming amount exceeds storage range")
    if item.get("double_spend_seen") is True:
        amount = 0
    return IncomingTransfer(
        amount_atomic=amount,
        confirmations=confirmations,
        txid=txid,
        account_index=subaddress[0],
        address_index=subaddress[1],
        height=height,
        timestamp=timestamp,
    )


def _aggregate_invoice_transfers(
    invoice: Invoice, transfers: list[IncomingTransfer]
) -> InvoiceObservation:
    matching = [
        transfer
        for transfer in transfers
        if transfer.account_index == invoice.xmr_account_index
        and transfer.address_index == invoice.xmr_address_index
        and transfer.amount_atomic > 0
    ]
    if not matching:
        return InvoiceObservation(0, 0, None)
    total = sum(transfer.amount_atomic for transfer in matching)
    if total > MAX_ATOMIC_UNITS:
        raise XmrTransferDataError("aggregate incoming amount exceeds storage range")

    ordered_by_confirmations = sorted(
        matching,
        key=lambda transfer: (transfer.confirmations, transfer.amount_atomic),
        reverse=True,
    )
    covered = 0
    coverage_confirmations = 0
    for transfer in ordered_by_confirmations:
        covered += transfer.amount_atomic
        coverage_confirmations = transfer.confirmations
        if covered >= invoice.expected_atomic:
            break
    if total < invoice.expected_atomic:
        coverage_confirmations = max(
            transfer.confirmations for transfer in matching
        )

    first = min(
        matching,
        key=lambda transfer: (
            transfer.height or 2**63 - 1,
            transfer.timestamp or 2**63 - 1,
            transfer.txid,
        ),
    )
    return InvoiceObservation(total, coverage_confirmations, first.txid)


def _matching_outgoing_txids(
    rows: list[dict[str, Any]],
    *,
    invoice: Invoice,
    attempt: SweepAttempt,
    cold_address: str,
) -> list[str]:
    matches: set[str] = set()
    earliest_timestamp = int((attempt.started_at - timedelta(minutes=1)).timestamp())
    for row in rows:
        if not isinstance(row, dict):
            raise XmrTransferDataError("outgoing transfer is not an object")
        indexes = _outgoing_subaddress_indexes(row)
        if (invoice.xmr_account_index, invoice.xmr_address_index) not in indexes:
            continue
        destinations = row.get("destinations")
        if not isinstance(destinations, list):
            raise XmrTransferDataError("outgoing transfer destinations are invalid")
        if not any(
            isinstance(destination, dict)
            and destination.get("address") == cold_address
            for destination in destinations
        ):
            continue
        timestamp = _optional_non_negative_int(
            row.get("timestamp", 0), "outgoing timestamp"
        )
        if timestamp and timestamp < earliest_timestamp:
            continue
        matches.add(_nonempty_text(row.get("txid"), "outgoing transaction ID"))
    return sorted(matches)


def _outgoing_subaddress_indexes(row: dict[str, Any]) -> set[tuple[int, int]]:
    indexes: set[tuple[int, int]] = set()
    single = _parse_subaddress_index(row.get("subaddr_index"))
    if single is not None:
        indexes.add(single)
    multiple = row.get("subaddr_indices", [])
    if not isinstance(multiple, list):
        raise XmrTransferDataError("outgoing subaddress indexes are invalid")
    for item in multiple:
        parsed = _parse_subaddress_index(item)
        if parsed is None:
            raise XmrTransferDataError("outgoing subaddress index is missing")
        indexes.add(parsed)
    return indexes


def _parse_subaddress_index(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise XmrTransferDataError("subaddress index is invalid")
    return (
        _non_negative_int(value.get("major"), "subaddress account index"),
        _non_negative_int(value.get("minor"), "subaddress address index"),
    )


def _single_sweep_txid(result: dict[str, Any]) -> str:
    if not isinstance(result, dict):
        raise XmrSweepUncertainError("sweep result is invalid")
    hashes = result.get("tx_hash_list")
    if not isinstance(hashes, list) or len(hashes) != 1:
        raise XmrSweepUncertainError("sweep result does not contain one transaction")
    try:
        return _nonempty_text(hashes[0], "sweep transaction ID")
    except XmrTransferDataError as exc:
        raise XmrSweepUncertainError("sweep transaction ID is invalid") from exc


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise XmrTransferDataError(f"{label} is invalid")
    return value


def _optional_non_negative_int(value: Any, label: str) -> int:
    return _non_negative_int(value, label)


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise XmrTransferDataError(f"{label} is invalid")
    return value


def _require_aware_datetime(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise XmrReconciliationError("poll time must be timezone-aware")
