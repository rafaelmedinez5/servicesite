# servicesite

Server-rendered Flask service catalog with XMR-only checkout, an authenticated
admin-managed catalog, purchase visibility, and manual fulfillment.

Tasks 0 through 3 establish the application boundary, source inventory, strict
configuration contract, isolated XMR wallet-RPC transport, fresh SQLite schema,
and canonical invoice domain. Admin authentication, web checkout, polling, and
production units remain deferred to their numbered tasks.

## Confirmed direction

- Python 3.12, Flask, Jinja/HTML, and CSS
- no JavaScript
- fresh SQLite database with WAL mode
- administrator-defined categories and services
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
  catalog.py              validated minimal category/service records
  config.py               typed startup and XMR configuration validation
  persistence.py          fresh SQLite schema and transactional repositories
  payments/
    invoice.py            canonical invoice creation and state machine
    xmr_wallet_rpc.py     isolated amount and wallet-RPC transport
  static/css/style.css    local CSS
  templates/index.html    scaffold landing page
deploy/systemd/           sanitized unit templates added in Task 6
docs/
  architecture.md
  decisions/application.md
  decisions/xmr-migration.md
  invoice-domain.md
  xmr-source-inventory.md
  xmr-wallet-rpc.md
scripts/                  operational scripts added in later tasks
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
- Do not install or start systemd/Tor/wallet services from this scaffold.
- A passing mocked or local test is not proof of production payment behavior.

## Next task

Proceed to Task 4: server-rendered service selection, private checkout/QR/status
pages, and route security. Do not connect to a production wallet or enable
polling yet.
