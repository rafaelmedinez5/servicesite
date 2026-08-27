# Servicesite architecture

Status: Tasks 0 through 7 plus the admin checkpoint. The validated wallet transport, minimal catalog
persistence, canonical invoice domain, fresh SQLite schema, and private web
checkout/status boundary are implemented. The protected reconciliation boundary
is implemented but not scheduled or production-verified. Sanitized deployment
units and a read-only deployment preflight exist. Single-admin catalog,
purchase visibility, and fulfillment are implemented; stagenet payment
verification remains a separate task.

## Runtime components

| Component | Responsibility | Boundary |
| --- | --- | --- |
| Public Flask app | Server-rendered catalog, checkout, and private status pages | `servicesite` user; loopback `127.0.0.1:5100` |
| Admin Flask routes | Purchase visibility and catalog management | Same process; authenticated, CSRF-protected admin session |
| SQLite | Catalog, purchase, invoice, and fulfillment records | Fresh DB under `/opt/servicesite/instance` with WAL mode |
| XMR poll job | Reconcile invoice transfers and confirmations | Separate oneshot/timer; protected internal interface |
| `monero-wallet-rpc` | Subaddresses, transfer observation, and approved sweep calls | Separate `xmrwallet` user; authenticated loopback RPC |
| Tor | Public onion routing | New hidden-service state; maps only to `127.0.0.1:5100` |

## Catalog and admin direction

The admin panel uses ordinary server-rendered forms without JavaScript.
The catalog model supports:

- categories with name, slug, description, publication state, and sort order;
- services assigned to categories with name, slug, description, USD price,
  optional duration, publication state, and sort order;
- archival instead of destructive deletion when historical purchases exist;
- immutable service/category/price snapshots on purchases;
- separate payment and manual-fulfillment states.

The first release uses one administrator account. First use creates the
password through a one-time form and stores only its generated hash in SQLite.
Password changes require the current password and rotate a credential version
that invalidates all admin sessions. An optional six-digit recovery PIN is read
from external configuration and provides an alternative login after password
setup. Password and PIN attempts share one SQLite-backed global rate limit.
Admin routes use fixed-expiry sessions, CSRF protection, and private/no-store
responses. Routine admin views omit bearer tokens, wallet addresses, and
transaction identifiers.

## Payment separation

Catalog administration does not mutate an open invoice. Checkout locks a USD
price snapshot, XMR conversion snapshot, and exact integer atomic amount. A
settled payment does not authorize any cybersecurity testing; engagement scope
and authorization remain separate records and operator checks.

## Task 3 persistence boundary

The fresh SQLite schema contains minimal category and service records plus
immutable invoice snapshots. Catalog publication/archive state and version are
rechecked transactionally when an invoice is inserted. The only invoice creation
entry point lives in `app/payments/invoice.py`; SQLite access remains isolated in
`app/persistence.py`.

The invoice locks its price in integer USD cents, XMR/USD rate as exact decimal
text, expected integer atomic amount, confirmation count, sweep policy, catalog
snapshots, and expiry. A later catalog edit cannot alter historical invoices.

## Task 4 web boundary

The public catalog and individual service-detail routes read only published,
non-archived categories and services. Service-detail pages use the unique,
validated service slug and return a generic 404 for unavailable records.
Checkout begins from the detail page and uses a signed-session CSRF token plus
a single-use form nonce, obtains a timestamped CoinGecko quote, and calls only
the canonical `InvoiceCreator`.
The route does not calculate amounts or create subaddresses itself.

Checkout, QR, and status resources require the invoice bearer token and return
`no-store`, `noindex`, no-referrer, frame-denial, and no-script headers. The QR
contains only a Monero URI with the unique address and numeric decimal amount.
Customer state mapping hides wallet indexes, transaction identifiers, internal
payment states, and sweep details. Only `settled` is described as eligible for
manual fulfillment review.

## Task 5 reconciliation boundary

The internal poll endpoint accepts only an actual loopback peer and an exact,
constant-time-compared internal token. It loads wallet incoming history for the
configured account, then matches each invoice using both account and subaddress
indexes. SQLite leases serialize invoice work across processes.

Sweep-required invoices persist an attempt and enter `sweeping_to_cold` before
the wallet call. The sweep transport performs one request only. A missing
response or malformed success stays uncertain until outgoing wallet history is
checked; a stored or reconciled transaction ID prevents another sweep. Summary
responses and logs contain counts and shortened invoice references, never
addresses, transaction IDs, tokens, or RPC response text.

## Task 6 process topology

The committed systemd templates keep Gunicorn, the one-shot HTTP poll client,
and the dedicated wallet-RPC in separate processes. Gunicorn and the poller run
as `servicesite`; wallet-RPC runs as `xmrwallet` and receives only a protected
external config-file path. A persistent calendar timer coalesces missed
one-minute polls. Filesystem write access is limited to the recorded instance and
log directories for each identity.

Gunicorn validates the configured loopback bind before opening its listener and
does not produce access logs containing private status-token URLs. The poll
client disables proxies for its loopback call and keeps the internal token out of
arguments and output. Wallet runtime validation remains a target-host operator
gate.

## Task 7 operational boundary

The deployment runbook pins and verifies a selected official Monero release,
separates mutable wallet state from root-owned binaries, keeps secrets in
separately owned `0600` files, creates a new Tor identity, initializes only a
fresh database, and preserves the legacy service during rollback.

The preflight has separate install and runtime expectations. It reads metadata
and validated configuration without displaying values, captures external command
output, compares reviewed unit files, probes only the two loopback ports, and
uses only fixed web health plus wallet `get_height`. It cannot create an invoice,
poll reconciliation, transfer/sweep XMR, alter a file, or operate a service.

## Task boundaries

- Task 0: scaffold and decisions only.
- Task 2: wallet-rpc transport and complete XMR configuration validation.
- Task 3: minimal catalog/invoice persistence and canonical invoice domain.
- Task 4: public catalog, checkout, QR, and private status views.
- Admin authentication, catalog management, purchase filters, and guarded manual fulfillment are implemented.
- Task 5: poll/confirm/sweep orchestration (implemented; staged verification pending).
- Task 6: sanitized systemd units, poll launcher, and install/rollback commands.
- Task 7: complete runbook plus read-only, redacting preflight.
- Tasks 8-10: staging, cutover, and reconciliation.
