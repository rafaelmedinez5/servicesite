# Servicesite architecture

Status: Tasks 0 through 4. The validated wallet transport, minimal catalog
persistence, canonical invoice domain, fresh SQLite schema, and private web
checkout/status boundary are implemented. Admin, polling, and deployment units
remain separate tasks.

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

The admin panel will use ordinary server-rendered forms without JavaScript.
The initial catalog model will support:

- categories with name, slug, description, publication state, and sort order;
- services assigned to categories with name, slug, description, USD price,
  optional duration, publication state, and sort order;
- archival instead of destructive deletion when historical purchases exist;
- immutable service/category/price snapshots on purchases;
- separate payment and manual-fulfillment states.

The first release uses one administrator account. The password is represented
only by a generated hash outside Git. Admin routes require session authentication,
CSRF protection, login rate limiting, and session expiry.

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

The public catalog reads only published, non-archived categories and services.
Checkout uses a signed-session CSRF token plus a single-use form nonce, obtains
a timestamped CoinGecko quote, and calls only the canonical `InvoiceCreator`.
The route does not calculate amounts or create subaddresses itself.

Checkout, QR, and status resources require the invoice bearer token and return
`no-store`, `noindex`, no-referrer, frame-denial, and no-script headers. The QR
contains only a Monero URI with the unique address and numeric decimal amount.
Customer state mapping hides wallet indexes, transaction identifiers, internal
payment states, and sweep details. Only `settled` is described as eligible for
manual fulfillment review.

## Task boundaries

- Task 0: scaffold and decisions only.
- Task 2: wallet-rpc transport and complete XMR configuration validation.
- Task 3: minimal catalog/invoice persistence and canonical invoice domain.
- Task 4: public catalog, checkout, QR, and private status views.
- Admin authentication and catalog CRUD remain a separate application task.
- Task 5: poll/confirm/sweep orchestration.
- Tasks 6-10: reproducible deployment, staging, cutover, and reconciliation.
