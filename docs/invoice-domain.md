# Invoice domain and persistence

Status: Tasks 3 and 5 complete. This is a local, tested domain, persistence, and
reconciliation layer; no production database or wallet was accessed.

## Fresh database boundary

`SQLiteDatabase.initialize()` creates schema version 6 with WAL mode and applies
mode `0600` to the database file. Initialization is idempotent for a recognized
`servicesite` database. If a database already contains tables but has no
`servicesite` schema marker, initialization refuses it instead of treating a
legacy database as compatible. Recognized schema versions 1 through 5 are
upgraded in place; version 3 adds fulfillment fields and the admin login guard,
version 4 adds the singleton administrator credential table, and version 5 adds
customer accounts and per-account login guards without altering payment
amounts, addresses, tokens, or transaction identifiers. Version 6 adds cart,
checkout-claim, order-ownership, and invoice-line tables. Existing account and
invoice rows are not rewritten or assigned inferred ownership.

The production parent directory must already exist with operator-reviewed
ownership and permissions. The application does not copy or inspect the legacy
database.

## Tables

### `categories`

Stores the minimum catalog category fields needed for later admin/public work:
identity, name, slug, description, publish/archive flags, sort order, and
timestamps.

### `services`

Stores category ownership, identity, name, slug, description, integer USD cents,
optional duration, publish/archive flags, sort order, a catalog version, and
timestamps. A service is purchasable only when both it and its category are
published and not archived.

### `invoices`

Stores:

- a 128-bit hexadecimal invoice ID and strong bearer status token;
- service/category identifiers, catalog version, and immutable name/description/
  duration snapshots;
- integer USD cents, exact XMR/USD `Decimal` text, rate source, and quote time;
- expected and observed integer atomic units;
- unique XMR address plus wallet account and subaddress indexes;
- required and observed confirmations;
- deposit and sweep transaction identifiers;
- the sweep policy locked for that invoice;
- payment status, customer-safe status note, and lifecycle timestamps.
- separate fulfillment status, optional internal note, and fulfillment time.

The database enforces unique status tokens, unique addresses, and unique
`(wallet account index, subaddress index)` pairs. Catalog rows referenced by
invoices cannot be deleted; later admin workflows archive them.

### `invoice_poll_claims` and `invoice_sweep_attempts`

Short-lived poll claims serialize one invoice across overlapping poll runs.
Sweep attempts persist a strong attempt token, start/update timestamps, and the
uncertain-response flag. These rows contain no wallet credentials or addresses.

### `admin_credentials` and `admin_login_guard`

The singleton credential row stores only a generated Werkzeug password hash,
credential version, and timestamps. The version increments on password change
so every older admin session becomes invalid. The separate login guard stores
only the global failure window and temporary block state.

### `customer_accounts` and `customer_login_guard`

Customer rows store a strong opaque ID, normalized unique username, generated
password hash, credential version, and timestamps. Plaintext passwords are
never persisted. The separate guard stores only the failed-login window and
temporary block for one existing customer account.

## Canonical creation path

`InvoiceCreator` is the only application invoice builder. Direct purchases use
`create_invoice(service_id, quote, customer_id=...)`; cart purchases use
`create_cart_invoice(...)`. Both call the same internal builder for amount
conversion, identity generation, wallet access, expiry, and confirmation policy.
It:

1. resolves and validates the selected purchasable services and quantities;
2. validates the rate as `Decimal`, enforces the configured maximum quote age,
   and calculates atomic units with exact decimal arithmetic, rounding upward
   only to the next indivisible atomic unit;
3. creates a strong invoice ID and bearer token;
4. requests a newly labeled wallet subaddress;
5. builds immutable service, category, price, rate, confirmation, sweep-policy,
   and expiry snapshots;
6. rechecks the catalog snapshot inside a SQLite `BEGIN IMMEDIATE` transaction;
7. inserts the invoice, ownership, and item snapshots in one transaction before
   returning it; cart checkout also clears exactly the claimed cart revision.

Cart totals are summed in integer USD cents before one XMR conversion and one
rounding operation. Each order receives one unique subaddress and follows the
existing invoice payment state machine. All items share the order's payment
and manual fulfillment state. The legacy invoice service/category ID fields
retain the first line's identifiers for compatibility; the authoritative
multi-service breakdown is in `invoice_items`.

If the catalog changes during creation, persistence aborts. If wallet access
fails, no database row is written. If a database insert fails after subaddress
creation, the transaction leaves no partial invoice, but the wallet may contain
an unused subaddress. Wallet subaddresses cannot participate in a SQLite
transaction; an unused address is safe and must never be reassigned.

## Locked pricing

Service price is stored as integer USD cents. XMR/USD rate input must be a
positive finite `Decimal`; binary floats are rejected. Expected atomic units are
calculated as:

`ceil((USD cents × 10^12) / (100 × USD per XMR))`

Later service-price or category/service text edits do not alter an existing
invoice. Task 4 uses a timestamped CoinGecko quote with a maximum age of 300
seconds and fails checkout when the approved quote is unavailable or stale.

## Payment state machine

The normal sweep-required path is:

`awaiting_payment -> paid_pending_confirmations -> paid_pending_sweep -> sweeping_to_cold -> settled`

An ordinary sweep failure may return `sweeping_to_cold` to
`paid_pending_sweep`. A sweep-required invoice cannot settle until a sweep
transaction identifier is stored.

When the invoice's locked sweep policy is disabled, the confirmed path is:

`awaiting_payment -> paid_pending_confirmations -> settled`

Only `awaiting_payment` and `paid_pending_confirmations` may transition to
`expired`. The expiry boundary is inclusive: `now >= expires_at`. Settled,
expired, and confirmed sweep-pending invoices cannot expire.

Entering confirmed/sweep/settled states requires observed atomic units to meet or
exceed the locked expectation and confirmations to meet or exceed the locked
requirement. Partial payment never settles; overpayment remains representable in
`observed_atomic` for later reconciliation.

## Task 5 behavior

The reconciliation service lists only open payment states, takes a per-invoice
SQLite lease, and matches transfers with both stored wallet indexes. It sums
integer atomic amounts and computes confirmation coverage over enough transfers
to satisfy the exact locked expectation. Zero, partial, exact, and overpayment
observations preserve the required state rules.

Sweep attempts are serialized and persisted before the wallet call. Confirmed
invoices cannot expire while waiting for or reconciling a sweep. Full operational
and recovery details are in `xmr-reconciliation.md`.

## Admin and fulfillment boundary

Admin catalog edits affect only future invoices because each purchase keeps its
immutable snapshots. Category and service removal is archival, not deletion.
The admin purchase list omits status bearer tokens, deposit addresses, and
transaction identifiers. A transactional guard permits fulfillment only when
the payment state is `settled`; fulfillment never changes payment state.

Production database initialization, online backups, and schema upgrades remain
operator actions described in the deployment runbook.
