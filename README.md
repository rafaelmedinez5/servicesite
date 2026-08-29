# servicesite

Server-rendered Flask service catalog with customer accounts, saved multi-service
carts, customer order history, account-gated XMR-only checkout, an authenticated
admin-managed catalog, purchase visibility, and manual fulfillment.

Tasks 0 through 7 establish the application boundary, source inventory, strict
configuration contract, isolated XMR wallet-RPC transport, fresh SQLite schema,
canonical invoice domain, private server-rendered checkout/status flow, and the
protected payment reconciliation boundary, sanitized deployment templates, and
a gated deployment/preflight/rollback runbook. The single-administrator catalog,
purchase, and manual-fulfillment workflow is implemented. Stagenet payment
verification and production cutover remain deferred.

## Confirmed direction

- Python 3.12, Flask, Jinja/HTML, and CSS
- no JavaScript
- fresh SQLite database with WAL mode
- administrator-defined categories and services
- privacy-sanitized administrator service-image uploads
- USD prices converted and locked into XMR at checkout
- separate payment and manual-fulfillment states
- Gunicorn bound to `127.0.0.1:5100`
- new Tor onion service
- dedicated `servicesite` and `xmrwallet` Linux identities
- no wallet-file access from the web application

## Repository map

```text
app/
  __init__.py             Flask application factory and Task 0 routes
  admin.py                authenticated catalog, purchase, and fulfillment routes
  customer_auth.py        customer registration, login, logout, and session loading
  orders.py               cart snapshots, quantities, and order line values
  shopping.py             saved cart and customer-owned order routes
  catalog.py              validated minimal category/service records
  config.py               typed startup and XMR configuration validation
  persistence.py          fresh SQLite schema and transactional repositories
  web.py                  catalog, checkout, QR, and private status routes
  web_security.py         CSRF and single-use checkout form tokens
  payments/
    invoice.py            canonical invoice creation and state machine
    xmr_rate.py           exact CoinGecko quote and freshness enforcement
    xmr_reconciliation.py transfer, confirmation, expiry, and sweep orchestration
    xmr_wallet_rpc.py     isolated amount and wallet-RPC transport
  internal.py             loopback-and-token-protected polling endpoint
  static/css/style.css    local CSS
  templates/index.html    public service catalog
  templates/service_detail.html  individual service information and purchase form
  templates/customer/     customer registration, login, and account views
deploy/systemd/           sanitized web, poll, timer, and wallet-RPC units
docs/
  architecture.md
  admin.md
  decisions/application.md
  decisions/systemd.md
  decisions/xmr-migration.md
  deploy-xmr.md
  invoice-domain.md
  systemd-install.md
  web-checkout.md
  customer-accounts.md
  cart-orders.md
  xmr-reconciliation.md
  xmr-source-inventory.md
  xmr-wallet-rpc.md
scripts/poll-xmr          token-safe loopback reconciliation launcher
scripts/preflight         read-only, redacting install/runtime deployment checks
tests/test_smoke.py
wsgi.py
```

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m pytest
```

Run the development server on the confirmed loopback port:

```bash
.venv/bin/flask --app wsgi:app run --host 127.0.0.1 --port 5100
```

Verify:

```bash
curl -fsS http://127.0.0.1:5100/health
```

The expected response is `OK`.

## Production boundaries

- Production configuration lives outside Git at
  `/etc/servicesite/servicesite.env` with mode `0600`.
- The application must never bind a public interface.
- Wallet files, passwords, internal tokens, databases, backups, and Tor private
  keys must never enter the repository.
- Do not install or start systemd/Tor/wallet services from this checkpoint.
- A passing mocked or local test is not proof of production payment behavior.

## Task 5 safety boundary

`POST /internal/poll-xmr` is available only from a loopback source with the
exact `X-Internal-Token`. It reconciles open invoices using both wallet account
and subaddress indexes, serializes work with SQLite leases, and persists sweep
attempt state before making a non-retried sweep call. See
`docs/xmr-reconciliation.md` for state, recovery, and residual-risk details.

## Task 6 deployment boundary

The committed units use separate least-privilege identities, protected external
configuration, restrictive filesystem access, bounded timeouts, and a persistent
one-minute timer. The polling launcher keeps the internal token out of process
arguments. Gunicorn access logging is disabled because bearer tokens occur in
private request paths.

The wallet-RPC binary is not installed, so its runtime and external configuration
remain blocked. No unit has been copied, enabled, or started.

## Task 7 deployment gate

`scripts/preflight` validates package state, recorded ownership/modes,
production settings without displaying values, the pinned Monero wallet-RPC
version/options, the exact Tor mapping, unit collisions/copies, loopback ports,
and harmless runtime health. Install mode expects unused ports/unit names;
runtime mode expects active web and wallet-RPC services. Neither mode writes a
file, changes a service, calls the payment poller, or mutates wallet/database
state.

The complete STAGING, PRODUCTION, ROLLBACK, and OPERATOR APPROVAL gates are in
`docs/deploy-xmr.md`. No live-host action was performed in Task 7.

## Administration

`/admin/login` provides the server-rendered single-administrator workflow. An
empty credential table presents a one-time password setup form; the resulting
Werkzeug hash is stored in the protected SQLite database. The security page
changes the password only after verifying the current password and invalidates
all existing admin sessions. An optional six-digit recovery PIN from external
configuration provides a second login path after password setup; password and
PIN failures share the same SQLite-backed global rate limit. Admin responses
are private/no-store, all state-changing forms require CSRF, catalog records are
archived instead of deleted, and fulfillment is rejected until payment is
settled. See `docs/admin.md`.

Service edit pages accept a JPEG, PNG, or WebP image. The server decodes it and
creates a new metadata-free WebP under the private instance directory with a
random name; the original filename and bytes are not retained. Public image
routes expose only the current sanitized derivative for a published service.
Visible identifying details in the pixels still require manual review.

## Customer cart and orders

Signed-in customers can add services, adjust quantities, and create one XMR
invoice for the whole cart. `/account` lists their 100 most recent orders;
order details remain ownership-checked. Direct single-service purchases also
appear in this history. Existing bearer payment links continue to work.

The cart release introduced the schema 5-to-6 migration. Existing accounts and
invoices are preserved. See `docs/cart-orders.md`.

## Current database migration

**Database migration required: schema 6 to 7.** Schema 7 adds only the nullable
random service-image key; image bytes remain outside SQLite. Back up the
database, stop the web service, install the updated requirements, run the
documented initializer, and then restart. See `docs/admin.md` and the exact
operator procedure in `docs/deploy-xmr.md`.

## Next task

Proceed to Task 8: execute the approved stagenet deployment and real payment
matrix. Do not enable production, fulfillment, or sweeping from mocked results.
