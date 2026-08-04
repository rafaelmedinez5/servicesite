# Codex taskbook: migrate the Monero payment system to a new service repository

## How to use this taskbook

Give Codex one numbered task at a time, in order. Keep each task in a separate branch or checkpoint. Do not combine migration, deployment, and cutover into one prompt.

Source reference:

- Repository: owner-provided private source repository (supply privately at execution time)
- Source ref: owner-provided pinned branch or commit (supply privately at execution time)
- Public placeholder: `<PINNED_PRIVATE_SOURCE_COMMIT>`
- Audit date: 2026-08-04

Do not commit the private source repository URL or exact commit to this public repository. Supply both to Codex in the private task conversation.

## What the source audit found

The existing Monero flow is real and reusable, but it is not a copy-and-paste subsystem yet:

1. `app/payments/xmr_wallet_rpc.py` is a small reusable JSON-RPC client. It supports digest authentication, retries, subaddress creation, incoming-transfer lookup, and `sweep_all`.
2. Most orchestration is embedded in the roughly 3,000-line `app/server.py`: invoice creation, status refresh, internal polling, confirmation checks, sweep attempts, and status transitions.
3. `app/db.py` persists XMR account/address indexes, expected and paid atomic amounts, deposit txid, sweep txid, confirmations, expiry, and settlement state.
4. `POST /internal/poll-xmr` requires loopback access and `X-Internal-Token`. `scripts/poll-xmr.sh` invokes it.
5. The poll script hard-codes `<LEGACY_INSTALL_DIR>` and `127.0.0.1:5000`; these values must not migrate unchanged.
6. The repository has no committed systemd service or timer units. The exact working units must be inventoried from the current VPS before it is changed or destroyed.
7. `.env.example` contains BTC settings but omits the documented XMR runtime and sweep settings. It is not a safe migration manifest.
8. `docs/xmr_sweep_note.md` says sweeping is not implemented, while the current code and tests do implement it. Treat code plus tests as the source of truth and update the new docs.
9. The source still has invoice-building logic in both `app/server.py` and `app/core/pricing.py`, despite a completed task claiming consolidation. The new repository must have one canonical builder.
10. The source default is 10 XMR confirmations and a 2-hour invoice TTL. Only pre-confirmation states expire.
11. `tests/test_xmr_sweep_status.py` covers normal sweep failure, retry, and stored-txid duplicate prevention. It does not fully prove safety when a sweep is broadcast but the RPC response is lost.
12. The source has no `AGENTS.md`.

## Decisions to record before implementation

Create `docs/decisions/xmr-migration.md` and record explicit values for:

| Decision | Recommended starting point |
| --- | --- |
| New repository/service name | User supplies it |
| Deployment topology | Same VPS or new VPS; do not leave ambiguous |
| Install directory | A new path, not `<LEGACY_INSTALL_DIR>` |
| Linux service user/group | Dedicated least-privilege identity |
| Web bind | A free loopback port, not the existing site's port |
| Onion service | New `HiddenServiceDir` and hostname |
| Database | Fresh application database |
| Old invoices | Keep old site/DB available until resolved |
| Wallet-rpc | Reuse the current loopback instance on the same VPS, or deploy a dedicated instance on a new VPS |
| Confirmations | 10 unless explicitly changed |
| Invoice TTL | 2 hours unless explicitly changed |
| Sweep policy | Disabled for initial stagenet verification; explicitly enable after sweep testing |
| Pricing policy | Record service price, rate source, quote age limit, and failure behavior |
| JavaScript policy | Server-rendered/no-JavaScript unless explicitly changed |

Recommended cutover model: start the new app with a fresh database and new onion service; keep `legacy service` running for old invoices. If both apps run on one VPS, give them different service names, database paths, web ports, poll timers, and onion directories. They may reuse one loopback wallet-rpc only after confirming that both persist and match their own subaddress indexes.

---

## Task 0 — Bootstrap the new repository and freeze decisions

```text
Goal: prepare this new repository for a controlled XMR-payment migration without implementing payment behavior yet.

Context:
- The read-only source is the owner-provided private source repository at commit <PINNED_PRIVATE_SOURCE_COMMIT>.
- Read the root AGENTS.md and CODEX_MIGRATION_TASKS.md first.

Do:
1. Inspect the new repository and preserve any existing work.
2. Create a minimal Python/Flask project scaffold, README, tests directory, docs directory, and deployment directories. Do not copy the old app wholesale.
3. Create docs/decisions/xmr-migration.md using the decision table in the taskbook.
4. Ask me only for decisions that cannot be discovered safely: service/repo name, same-VPS versus new-VPS, service price, and whether the current wallet-rpc will be reused.
5. Add a defensive .gitignore covering .env files, virtual environments, caches, instance databases, backups, wallet files/keys, password files, Tor hidden-service keys, and generated logs.
6. Add a safe .env.example containing names/placeholders only; Task 2 will complete the XMR block.

Constraints:
- No GitHub push, systemd/Tor changes, service starts, wallet access, database copy, or secret handling.
- Do not pick a production port or path silently; record the chosen value.

Done when:
- The scaffold imports successfully.
- A smoke test passes.
- The decision document exists and unresolved decisions are visibly marked BLOCKED.
- git diff contains no secret or production data.

Return: files changed, tests run/results, decisions recorded, and blockers. Stop after this task.
```

## Task 1 — Inventory the working source deployment, read-only

```text
Goal: produce a sanitized source-of-truth inventory of the existing XMR deployment before recreating any units or timers.

Inspect read-only:
- the owner-provided private source repository at commit <PINNED_PRIVATE_SOURCE_COMMIT>
- app/payments/xmr_wallet_rpc.py
- XMR portions of app/server.py and app/db.py
- scripts/poll-xmr.sh
- tests/test_xmr_sweep_status.py
- README XMR/config/deployment sections

For the current VPS, inspect (do not modify) the effective definitions and runtime metadata for:
- the Flask/Gunicorn service
- the XMR polling oneshot service and timer
- monero-wallet-rpc service, if systemd-managed
- Tor service/onion mapping

Use read-only commands such as systemctl cat/show, systemctl list-timers, ss, ps, and journalctl. Never print Environment values, RPC credentials, internal tokens, wallet paths beyond the minimum needed, wallet addresses, transaction IDs, or Tor private keys. If you cannot access the VPS, create a redacted command checklist for me and mark the inventory blocked instead of inventing unit contents.

Write docs/xmr-source-inventory.md with:
- exact source commit
- component diagram in prose
- service/timer names and schedules
- users/groups, working directories, bind addresses, ports, and environment-file paths with values redacted
- wallet-rpc binary version and supported flags
- daemon connection topology (local/public/onion) without credentials
- database path and ownership, not database contents
- current health-check commands
- every hard-coded old-service value that must change
- gaps or contradictions between code, docs, and live deployment

Constraints:
- Source repository and VPS are read-only.
- Do not copy live unit files until they are sanitized.
- Do not change permissions, restart services, or run wallet RPC methods that mutate the wallet.

Done when:
- The inventory distinguishes verified facts from assumptions.
- All systemd/timer information is either captured from the VPS or marked BLOCKED.
- No sensitive values appear in the document or terminal summary.

Return: inventory path, verified topology, redacted blockers, and no implementation changes. Stop after this task.
```

## Task 2 — Extract the wallet-rpc client and configuration contract

```text
Goal: add a small, tested XMR wallet-rpc transport layer and complete configuration validation.

Use app/payments/xmr_wallet_rpc.py in the private source repository at the audited commit as behavior reference. Migrate only the required ideas: atomic conversion, digest auth, retry/backoff, create_address/subaddress, get_height, incoming transfers, transfer lookup, and sweep_all.

Implement:
- a focused wallet-rpc client module with injected session/config where practical for testing
- typed or clearly structured settings loaded once at startup
- explicit timeout, bounded retries, and JSON-RPC/HTTP/schema error handling
- Decimal/string to atomic conversion with validation and no floats
- startup validation that production wallet-rpc is loopback/private unless an explicit dangerous override is documented
- a complete .env.example XMR block containing URL, user, password placeholder, account index, timeout/retries/backoff, confirmation count, sweep enabled, cold-address placeholder, sweep account, priority, relay, internal token placeholder, DB path, app bind, and app port

Do not include actual credentials, addresses, daemon URLs, wallet paths, or old service names. Do not connect to the production wallet in tests.

Tests:
- conversion precision and invalid inputs
- digest auth attachment
- retry then success
- bounded failure
- JSON-RPC error response
- malformed response
- subaddress result parsing

Done when the focused tests and full current suite pass, and the module has no Flask or database imports.

Return: design summary, configuration names, tests/results, and known differences from the source client. Stop after this task.
```

## Task 3 — Implement the canonical invoice domain and persistence

```text
Goal: implement one XMR invoice model, one creation path, and transactional persistence for the new service.

Implement a minimal schema containing:
- invoice ID and strong bearer status token
- service/product identifier and locked price/rate snapshot needed for audit
- expected and observed XMR atomic amounts
- XMR address, wallet account index, and subaddress index
- required/observed confirmations
- deposit txid and sweep txid
- status and customer-safe status note
- created_at, expires_at, expired_at, settled_at, and updated_at

Implement one canonical create-invoice service that:
- validates the selected service and price
- obtains a unique wallet-rpc subaddress labeled with the invoice ID
- locks the exact atomic amount and confirmation count
- persists the invoice atomically before returning checkout data
- never stores wallet credentials or private keys

Implement and enforce this state model:
awaiting_payment -> paid_pending_confirmations -> paid_pending_sweep -> sweeping_to_cold -> settled
With sweeping disabled, confirmed payment may go directly to settled. Only awaiting_payment and paid_pending_confirmations may expire.

Constraints:
- Use a fresh database by default; do not import the legacy service database.
- Do not copy unrelated legacy service invoice columns or business flows.
- Do not implement the same builder in both a route and a pricing module.
- All money is integer atomic units internally.

Tests must cover unique subaddresses, locked amounts, transaction rollback on failure, legal/illegal state changes, expiry boundaries, and fresh-schema initialization.

Done when focused tests and the full suite pass and there is exactly one callable invoice-creation path.

Return: schema/state summary, tests/results, and migration decisions that remain open. Stop after this task.
```

## Task 4 — Add the service checkout, QR, and private status flow

```text
Goal: connect the service page to the canonical XMR invoice service without changing the payment internals.

Implement server-rendered routes/templates for:
- service selection/order submission
- CSRF-protected XMR invoice creation
- checkout page showing the locked XMR amount, unique subaddress, expiry, invoice ID, and a Monero payment QR
- token-protected status URL and status page
- rate-limited manual status refresh, if retained

Requirements:
- Validate all service IDs and inputs server-side.
- The Monero URI must contain a numeric decimal amount, not an amount string with an XMR suffix.
- Status and QR resources require the invoice-specific bearer token.
- Add noindex/no-store headers to checkout and status resources.
- Do not expose RPC errors, wallet metadata, account/subaddress indexes, internal tokens, txids, or sweep details to customers.
- The purchased service is fulfilled only after status=settled.
- If the pricing rate is unavailable or stale beyond the recorded policy, fail checkout safely; do not use an indefinite hard-coded production fallback.
- Keep the no-JavaScript policy unless docs/decisions/xmr-migration.md explicitly changes it.

Tests:
- CSRF and input validation
- one invoice per successful submit
- token enforcement for checkout/QR/status
- correct Monero URI amount
- partial/expired/pending/settled customer states
- no fulfillment before settled
- security and cache headers

Done when focused tests and the full suite pass and no route duplicates invoice-building logic.

Return: route map, customer state behavior, tests/results, and screenshots only if visual verification is available. Stop after this task.
```

## Task 5 — Port and harden the poll/confirm/sweep orchestrator

```text
Goal: implement the background XMR reconciliation behavior with safe retry and no silent duplicate sweep.

Use the audited legacy service internal_poll_xmr behavior and tests as reference, but isolate it in a payment service that can be called by one protected internal endpoint or a management command.

Implement:
- list only this application's open XMR invoices
- fetch wallet transfers for the configured account
- match by both account and subaddress index
- sum matching incoming amounts and preserve the deposit txid/observed confirmations
- handle zero, partial, exact, and overpayment
- enforce the configured confirmation threshold
- expire only pre-confirmation states
- with sweep disabled: settle after full payment and confirmations
- with sweep enabled: transition through pending/in-progress, sweep only the invoice subaddress to the configured cold destination, persist the returned txid, then settle
- on failure: remain non-settled with a retryable, operator-visible state
- reconcile an uncertain previous attempt before retrying, including the case where broadcast succeeded but the RPC response was lost
- serialize or lock per-invoice processing so overlapping requests cannot sweep twice
- emit sanitized operational logs/metrics without credentials, tokens, full addresses, or txids

If using HTTP, preserve both controls: loopback source and constant-time comparison of X-Internal-Token. If using a management command, ensure the systemd service can call it without exposing secrets in argv.

Tests:
- no transfers, partial, exact, overpayment
- low then sufficient confirmations
- RPC unavailable
- sweep disabled
- sweep success and ordinary failure/retry
- existing sweep txid prevents another call
- concurrent poll attempts
- broadcast-success/response-loss reconciliation
- confirmed sweep-pending invoice does not expire
- internal endpoint rejects non-loopback and missing/wrong token

Do not call mainnet wallet-rpc or enable a timer in this task.

Done when all payment tests and the full suite pass and the code documents what idempotency can and cannot guarantee.

Return: state transitions, locking/reconciliation design, tests/results, and any residual risk. Stop after this task.
```

## Task 6 — Commit sanitized systemd units, timer, and scripts

```text
Goal: make deployment reproducible in Git without touching the live services.

Use docs/xmr-source-inventory.md and docs/decisions/xmr-migration.md. Do not invent live flags that were not verified against the installed monero-wallet-rpc version.

Create under deploy/systemd/:
- the new Gunicorn/Flask service unit
- an XMR polling oneshot service
- an XMR polling timer
- a wallet-rpc unit only if the decision is to deploy a dedicated instance; if reusing the current instance, document that and do not create a conflicting unit

Create/update scripts so they derive paths/ports from the new service configuration. Remove every <LEGACY_INSTALL_DIR>, legacy service unit name, old database path, and fixed port 5000 reference.

Unit requirements:
- dedicated least-privilege user/group
- explicit WorkingDirectory and EnvironmentFile for the new service
- loopback web bind
- ordering on network availability and the app/wallet-rpc where appropriate
- bounded start timeout and useful restart policy
- restrictive umask and safe hardening compatible with the verified writable DB/log paths
- timer interval recorded in the decision doc; default proposal is one minute
- no secrets embedded in unit files or command-line examples
- no duplicate wallet-rpc when the existing loopback service is shared

Add docs/systemd-install.md with install, daemon-reload, enable/start, status, timer, logs, rollback, and uninstall commands. Commands that mutate the live host must be clearly labeled OPERATOR ACTION.

Validate unit syntax with systemd-analyze verify when available, but do not copy units to /etc/systemd/system, daemon-reload, enable, start, restart, or stop anything.

Done when syntax/static checks and all tests pass, source-inventory decisions are reflected, and a repository search finds no old hard-coded service values.

Return: unit topology, validation results, operator steps not executed, and blockers. Stop after this task.
```

## Task 7 — Write the deployment, secret, wallet, Tor, and rollback runbook

```text
Goal: create a complete operator runbook for either the recorded same-VPS or new-VPS topology.

Write docs/deploy-xmr.md covering:
- OS packages, Python environment, Gunicorn, Redis if used, Tor, and the verified Monero binaries
- directory creation and ownership
- environment-file creation from .env.example without displaying secret values
- permissions: secret files 0600 and private directories 0700 or stricter as appropriate
- fresh database initialization and backup/restore
- wallet-rpc topology, digest auth, loopback/private bind, daemon/Tor connectivity, and health checks
- manual wallet-file transfer only when using a new VPS; never place wallet bytes or passwords in Git and never automate seed/private-key extraction
- new Tor HiddenServiceDir and mapping to the new loopback port; never reuse/copy the old onion private key by default
- systemd installation and dependency order
- sanitized health, timer, and journal checks
- backup and rollback that leaves legacy service and old invoices intact
- explicit same-VPS port/unit/path collision checks

Add a preflight script that performs read-only checks and prints no secrets. It may verify files exist and permissions are restrictive, ports are available, units are known, wallet-rpc responds to a harmless method, the DB directory is writable by the service user, and Tor config references the intended port. It must not create an invoice, sweep, transfer, restart a service, or modify Tor/systemd.

Done when the runbook has separate STAGING, PRODUCTION, ROLLBACK, and OPERATOR APPROVAL gates and the preflight script has tests or safe dry-run verification.

Return: runbook path, preflight checks/results, and all manual secret/wallet actions. Stop after this task.
```

## Task 8 — Stage and verify end to end; do not cut over

```text
Goal: validate the complete flow in a safe staging environment without changing production or sending mainnet funds.

First inspect git status, all prior task acceptance results, and the decision/inventory/runbook docs. If no stagenet wallet/daemon is available, stop and report the blocker; do not substitute the production wallet.

With explicit operator approval for staging actions:
- install/start only the staging-named services and timer
- verify loopback binds and no collisions with legacy service
- create a stagenet invoice through the service UI
- verify unique subaddress, QR/URI, tokenized status, partial payment, confirmation progression, sweep-disabled settlement, and service fulfillment
- enable sweep only in staging, then verify cold sweep success
- simulate wallet-rpc outage and sweep failure, confirm safe retry and no duplicate sweep
- reboot/restart simulation if approved and verify timer persistence and recovery
- inspect sanitized logs, health, metrics, and database state
- run the full automated test suite

Do not edit production systemd/Tor config, do not use production secrets, do not send mainnet XMR, and do not stop legacy service.

Write docs/staging-verification.md with timestamped evidence, redacted commands, results, failures, and unresolved risks. Never include addresses, tokens, credentials, or txids.

Done when every staging acceptance case passes or is explicitly marked failed/blocked. A mocked test alone is not end-to-end proof.

Return: PASS/FAIL matrix and a production go/no-go recommendation. Stop; do not cut over.
```

## Task 9 — Production cutover (approval-gated)

```text
Goal: execute the reviewed production runbook only after I explicitly approve the exact target host, units, ports, paths, onion service, wallet-rpc topology, and rollback point.

Before mutation, show a concise preflight table and stop for approval if any value is ambiguous. Confirm:
- backups are current and restorable
- legacy service remains available for old/open invoices
- new units/port/DB/onion directory do not collide
- secrets and permissions are in place without printing values
- wallet-rpc health and sync state are acceptable
- staging verification passed
- rollback commands and trigger thresholds are ready

After explicit approval, follow docs/deploy-xmr.md exactly. Start dependencies in order, verify health after each step, enable the polling timer, verify the new onion route, and create only an operator-approved production smoke invoice. Do not send funds unless separately approved.

Abort and roll back on wallet-rpc degradation, repeated poll failures, incorrect invoice amounts/addresses, unexpected public binds, timer storms, database errors, or secret leakage.

Do not remove the old app, database, wallet, units, onion service, or backups in this task.

Return: exact actions taken, sanitized verification results, current rollback status, and a monitoring checklist. Stop after cutover verification.
```

## Task 10 — Post-cutover reconciliation and later cleanup

```text
Goal: reconcile the new service after an observation period and propose cleanup without deleting anything.

Inspect read-only:
- new invoice state counts and sweep-pending backlog
- timer/service health and restart counts
- sanitized error logs and metrics
- old legacy service open XMR invoices and operational dependencies
- backups and rollback readiness

Write docs/post-cutover-review.md with findings and a cleanup proposal. Do not stop, disable, delete, archive, rotate, or move the old service, database, wallet, onion key, units, or backups.

Only recommend old-service retirement after there are no unresolved invoices and the operator confirms retention requirements. List every destructive step separately for a future approval-gated task.

Return: reconciliation summary, unresolved invoices/errors, keep/retire recommendation, and proposed approval-gated cleanup steps. Stop without mutations.
```

## Operator reminder

The most important action before changing the old VPS is Task 1: capture the effective unit and timer definitions in sanitized form. They are not present in the GitHub repository, so they cannot be reconstructed exactly from source control alone.
