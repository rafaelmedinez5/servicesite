# XMR migration decisions

Status: VPS architecture decisions recorded. Application implementation and deployment remain separate approval gates.

## Product

`servicesite` is a service sales site for authorized cybersecurity engagements. The initial service offering is red-team / blue-team security assessment work in which a client authorizes testing of its systems and receives a detailed findings report, exploit analysis, risk assessment, and mitigation guidance.

The website is a sales/intake/payment front end. It is not itself an autonomous penetration-testing platform. Any client engagement must have explicit scope and authorization before testing begins.

## Deployment

- VPS provider: **Shinjiru**.
- OS: **Ubuntu 24.04 LTS, x86_64**.
- Deployment topology: **new VPS**.
- Administration: **HTML5 serial console** supplied by the VPS provider.
- SSH: **not used for routine server administration**. If GitHub SSH credentials are configured, they are for GitHub repository access only and must not be confused with server-management access.
- The legacy `salessite` VPS/application remains untouched during development and cutover.
- The new application gets a new Linux service identity, install directory, database path, web port, systemd service/timer names, and Tor onion service.
- Database: **SQLite** for the initial application.
- The new application will use a fresh database. Legacy invoices remain with the legacy application until resolved.

## Proposed filesystem layout

Use `/opt/servicesite` as the application root unless a later deployment decision changes it:

- `/opt/servicesite/app` — application source/runtime code
- `/opt/servicesite/venv` — Python virtual environment
- `/opt/servicesite/instance` — SQLite database and instance data
- `/opt/servicesite/scripts` — operational scripts
- `/opt/servicesite/logs` — application-specific logs only if needed
- `/opt/servicesite/secrets` — application-local secret files, restricted and never committed
- `/etc/servicesite/servicesite.env` — production environment configuration, mode 0600

Keep wallet files outside the application tree so the web application user cannot read them.

## Linux identities

Use separate least-privilege identities:

- `servicesite` — Flask/Gunicorn application and application-owned SQLite data
- `xmrwallet` — dedicated `monero-wallet-rpc` process and wallet files
- `debian-tor` — system Tor service

Do not add `servicesite` to the `xmrwallet` group. Do not grant either service account sudo privileges. Administrative system changes are performed through the serial-console administrator/root workflow.

## Monero

- A **new `monero-wallet-rpc` instance** will be created for `servicesite`.
- Do not reuse the legacy wallet-rpc process, wallet files, wallet database, or systemd unit.
- Wallet-rpc must bind to loopback/private networking and require authentication.
- The new wallet must be initialized and backed up according to the deployment runbook. Seeds/private keys/passwords never enter Git.
- Default confirmation requirement: **10 confirmations** unless explicitly changed later.
- Initial staging policy: **sweep disabled** until end-to-end reconciliation has been verified. Production sweep behavior is an explicit later decision.
- Invoice amounts are stored internally as integer atomic units.
- Each invoice receives a unique Monero subaddress.

## Application behavior

The service checkout should be simple and server-rendered. The payment flow should create an invoice, display the exact locked XMR amount and unique address/QR, and provide a private status page. Fulfillment is not considered complete until the configured payment lifecycle reaches `settled`.

The XMR payment subsystem should remain independent from the cybersecurity engagement workflow. Payment confirms that a purchase/engagement request has been paid; it must not automatically authorize a security test or grant access to a target. Engagement authorization and scope are separate business records/processes.

## Tor onion service

- Create a **new dedicated onion service** for `servicesite`.
- Do not reuse or copy the legacy site's onion private key.
- Tor should expose only the application's loopback listener through `HiddenServicePort`.
- The application and wallet-rpc ports remain inaccessible from the public network.
- Onion private keys remain on the VPS under Tor's protected state directory and never enter Git.

## Security-service governance baseline

Before an engagement is accepted for testing, the service workflow should eventually capture:

- client identity/contact and authorized representative
- explicit authorization / signed engagement reference
- in-scope domains, IP ranges, applications, cloud resources, and exclusions
- permitted testing techniques and prohibited actions
- testing window and timezone
- production/critical-asset restrictions
- emergency contact and escalation procedure
- evidence/reporting requirements
- engagement identifier and immutable scope snapshot

The platform should deny ambiguous or incomplete scope rather than treating payment as authorization.

## Still to decide

- final service name/brand
- final service packages and pricing
- XMR pricing/rate source and quote-age policy
- application loopback port
- exact Linux service account naming, if different from the proposed names above
- new onion-service deployment details
- new wallet account/index strategy
- cold-wallet sweep destination and sweep policy
- retention period for engagement/payment records
- exact report format and customer delivery workflow

These are not to be guessed by an agent. Record them when decided.
