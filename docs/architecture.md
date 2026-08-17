# Servicesite architecture

Status: Task 0 scaffold. Catalog, admin, persistence, XMR, and deployment units
are intentionally not implemented yet.

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

## Task boundaries

- Task 0: scaffold and decisions only.
- Task 2: wallet-rpc transport and complete XMR configuration validation.
- Task 3: catalog/purchase/invoice persistence and canonical invoice domain.
- Task 4: public catalog, admin authentication/CRUD, checkout, and status views.
- Task 5: poll/confirm/sweep orchestration.
- Tasks 6-10: reproducible deployment, staging, cutover, and reconciliation.
