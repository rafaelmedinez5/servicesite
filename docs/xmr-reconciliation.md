# XMR reconciliation and sweep safety

Status: Task 5 complete in code and automated tests. No production wallet,
mainnet funds, live timer, or systemd service was used.

## Internal boundary

`POST /internal/poll-xmr` requires both:

- an actual IPv4 or IPv6 loopback source as reported by the web server; and
- the exact `X-Internal-Token`, compared in constant time.

Forwarded-address headers are ignored. Denials, wallet outages, and poll summaries
are sanitized. Private responses use `no-store`; tokens must never appear in a
URL, log, command history, or repository.

The endpoint is an operational mutation boundary, not a public health check. A
Task 6 oneshot job may call it over `127.0.0.1:5100`; customers cannot trigger a
wallet refresh.

## Observation and state rules

Each poll reads only open invoice states, synchronously refreshes the wallet,
and then fetches incoming transfers for the configured wallet account. An
invoice accepts a transfer only when both its account index and unique
subaddress index match. Amounts are summed as integer atomic units. The stored
confirmation value represents the lowest confirmation count among the transfers
needed to cover the locked expected amount.

- zero payment remains awaiting until expiry;
- a partial payment never settles;
- exact and overpayment can settle, while preserving the observed total;
- insufficient confirmations remain pending;
- only awaiting and pending-confirmation invoices expire;
- a confirmed invoice waiting for sweep never expires;
- when the invoice's locked sweep policy is disabled, confirmation may settle it
  directly;
- fulfillment remains unavailable until payment status is `settled`.

A wallet refresh failure, wallet-history outage, or malformed history aborts the
poll before observations or expiry state are changed. Cached history is never
used as proof that an invoice received no payment. After connectivity recovers,
a confirmed payment visible in the refreshed history can settle even when the
invoice's original expiry time has passed. Per-invoice SQLite leases keep
overlapping processes from working the same invoice; expired leases recover
after a crashed poller.

## Sweep protocol

For an invoice whose locked policy requires sweeping:

1. The repository atomically changes `paid_pending_sweep` to
   `sweeping_to_cold` and persists a unique attempt token.
2. `sweep_all` targets only the invoice subaddress. The transport makes exactly
   one request and never automatically retries this non-idempotent call.
3. A valid single returned transaction ID is stored before settlement.
4. An explicit wallet JSON-RPC rejection returns the invoice to the retryable
   pending state without settlement.
5. A timeout, connection loss, malformed success, crash, or missing response
   keeps the invoice in `sweeping_to_cold`.
6. Later polls search outgoing wallet history for the same account/subaddress,
   configured cold destination, and attempt time window. Exactly one match is
   stored and settled; multiple candidates remain operator-visible and blocked.
7. No additional sweep is issued while an attempt or transaction ID is stored.

The cold destination and sweep account configuration must not be changed while
an uncertain attempt exists. If the destination is unavailable, the invoice
remains blocked in `sweeping_to_cold`.

## Residual idempotency risk

Monero wallet-RPC does not accept an application idempotency key for `sweep_all`.
After `XMR_SWEEP_RECONCILE_SECONDS` (default 300), an uncertain attempt with no
matching outgoing history is released to `paid_pending_sweep`; only a later poll
can retry it. Absence from wallet history is not mathematical proof that the
first call never broadcast. A delayed or incomplete wallet history could still
produce a duplicate attempt.

For staging and initial production rollout, keep `XMR_SWEEP_ENABLED=false` until
wallet synchronization, outgoing-history visibility, response-loss recovery, and
the chosen delay are verified with an approved stagenet wallet. Enabling sweep is
an explicit operator decision. Back up the database before changing this policy,
monitor all `sweeping_to_cold` errors, and stop automated polling for manual
reconciliation if an attempt is ambiguous.

## Sanitized observability

The endpoint returns only counters: open, processed, locked, expired, partial,
pending-confirmation, pending-sweep, sweeping, settled, reconciled, and errors.
Logs contain an event name and an eight-character invoice reference. They do not
contain full invoice IDs, addresses, status tokens, transaction IDs, cold
destinations, credentials, RPC response bodies, or remote error messages.

## Verification boundary

Automated tests cover zero/partial/exact/overpayment, confirmation coverage,
expiry, account-plus-subaddress matching, refresh and wallet-history outages,
late confirmed payment recovery, disabled sweep, success, explicit rejection
and retry, stored transaction IDs, response loss, malformed success, outgoing
reconciliation, delayed retry, concurrent poll exclusion, and
loopback-plus-token enforcement. These fakes prove application behavior only;
they do not prove wallet version compatibility or production fund safety.
