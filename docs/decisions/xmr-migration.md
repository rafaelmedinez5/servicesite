# XMR migration decisions

Status: Task 0 decisions recorded. Implementation and deployment remain separate approval gates.

## Product

`servicesite` is a service sales site for authorized cybersecurity engagements. The initial service offering is red-team / blue-team security assessment work in which a client authorizes testing of its systems and receives a detailed findings report, exploit analysis, risk assessment, and mitigation guidance.

The website is a sales/intake/payment front end. It is not itself an autonomous penetration-testing platform. Any client engagement must have explicit scope and authorization before testing begins. OWASP's current autonomous penetration-testing guidance emphasizes machine-readable rules of engagement, target and time boundaries, authorization evidence, and continuous scope enforcement; those principles are useful governance requirements even where testing is performed by human operators. citeturn0search0turn0search6

## Deployment

- Deployment topology: **new VPS**.
- The legacy `salessite` VPS/application remains untouched during development and cutover.
- The new application gets a new Linux service identity, install directory, database path, web port, systemd service/timer names, and Tor onion service.
- The new application will use a fresh database. Legacy invoices remain with the legacy application until resolved.

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

The platform should deny ambiguous or incomplete scope rather than treating payment as authorization. OWASP's authorization guidance also recommends deny-by-default, least privilege, authorization checks on every protected request, and tests for authorization logic. citeturn0search3turn0search11

## Still to decide

- final service name/brand
- final service packages and pricing
- XMR pricing/rate source and quote-age policy
- new VPS provider and OS version
- application loopback port
- Linux service user/group names
- new onion-service naming and deployment details
- new wallet account/index strategy
- cold-wallet sweep destination and sweep policy
- retention period for engagement/payment records
- exact report format and customer delivery workflow

These are not to be guessed by an agent. Record them when decided.
