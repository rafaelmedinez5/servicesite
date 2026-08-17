# XMR migration decisions

Status: Task 0 decisions frozen on 2026-08-17. Payment implementation and
deployment remain separate approval gates.

## Product

`servicesite` is a service sales site for authorized cybersecurity engagements.
The initial direction is red-team / blue-team security assessment work in which
a client authorizes testing of its systems and receives findings, exploit
analysis, risk assessment, and mitigation guidance.

The website is a catalog, sales, intake, and payment front end. It is not an
autonomous penetration-testing platform. Payment never constitutes authorization
to begin testing.

## Confirmed application decisions

- Backend: Python 3.12 and Flask.
- Frontend: server-rendered HTML/Jinja and CSS; no JavaScript.
- Web bind: `127.0.0.1:5100`.
- Catalog: categories and services are managed from an authenticated admin panel.
- Public visibility: only published categories and services appear publicly.
- Pricing input: the administrator enters prices in USD.
- Checkout pricing: USD is converted to XMR and locked when an invoice is created.
- Historical integrity: each purchase stores immutable service, category, USD
  price, rate, and XMR amount snapshots.
- Fulfillment: manual for the initial release and recorded separately from payment.
- Catalog deletion: archive/unpublish rather than destroy records referenced by
  purchases.
- Authentication: one administrator account initially; only a password hash is
  stored outside Git.
- Admin forms: session-authenticated, CSRF-protected, rate-limited, and
  server-rendered.
- Database: fresh SQLite database with WAL mode.

## Deployment

- VPS provider: **Shinjiru**.
- OS: **Ubuntu 24.04 LTS, x86_64**, in an OpenVZ container.
- Deployment topology: **new VPS**.
- Administration: **HTML5 serial console** supplied by the VPS provider.
- The legacy application and its invoices remain untouched.
- Application root: `/opt/servicesite`.
- Source checkout: `/opt/servicesite/app`.
- Python virtual environment: `/opt/servicesite/.venv`.
- Instance data: `/opt/servicesite/instance`.
- Production environment file: `/etc/servicesite/servicesite.env`, mode `0600`.
- Application identity: `servicesite:servicesite`, without sudo or login shell.

The provider image has a known unresolved `tzdata`/`dpkg` configuration issue.
Application work may continue inside the isolated virtual environment, but the
package state remains a deployment risk that must be recorded and re-evaluated
before production approval.

## Monero

- A new, dedicated `monero-wallet-rpc` instance will be created.
- Wallet root: `/opt/monero`.
- Wallet identity: `xmrwallet:xmrwallet`, without sudo or login shell.
- `servicesite` is not a member of the `xmrwallet` group and cannot read wallet files.
- Wallet-rpc binds to loopback/private networking and requires digest authentication.
- Default confirmation requirement: 10 confirmations.
- Invoice amounts are integer atomic units internally.
- Every invoice receives a unique subaddress.
- Initial stagenet policy: sweep disabled until reconciliation is verified.
- Production sweep behavior remains an explicit later approval.

## Tor onion service

- Create a new onion service and new hidden-service directory.
- Map `HiddenServicePort` only to `127.0.0.1:5100`.
- Do not reuse or copy the legacy onion private key.
- Application and wallet-rpc ports remain inaccessible from the public network.

## Security-service governance baseline

Before an engagement is accepted for testing, the workflow should eventually
capture the authorized representative, signed authorization reference, scope and
exclusions, permitted and prohibited techniques, testing window, critical-asset
restrictions, emergency contact, evidence requirements, and immutable scope
snapshot. Ambiguous or incomplete scope must be denied.

## Still blocked

- final brand and initial catalog contents;
- exact wallet-rpc port and daemon/onion endpoint;
- wallet account/index strategy;
- cold-wallet destination and production sweep policy;
- engagement, purchase, and payment retention periods;
- report format and customer delivery workflow;
- final onion hostname, which is generated only during deployment.

Catalog contents and service prices are not implementation blockers because the
administrator will create them after the catalog workflow exists. The remaining
items must not be guessed by an agent.
