# Repository agent instructions

## Purpose

This repository is a server-rendered service sales site with an XMR-only payment flow. It is being built as a new application while preserving the proven Monero behavior from the owner-provided private source repository at a privately supplied pinned commit.

The public application, the XMR polling job, and `monero-wallet-rpc` are separate operational components. Treat payment correctness and secret isolation as release-blocking concerns.

## Start every task this way

1. Read `README.md`, this file, and any task-specific file under `docs/`.
2. Inspect `git status` and preserve unrelated user changes.
3. Restate the requested scope and acceptance criteria before editing.
4. Inspect the relevant implementation and tests. Do not assume the old repository documentation matches its code.
5. Make the smallest coherent change, run the relevant tests, and report what remains unverified.

Do not advance to the next item in `CODEX_MIGRATION_TASKS.md` unless the current task's acceptance criteria pass.

## Migration rules

- Treat the legacy repository as a read-only reference. Never commit to it as part of this migration.
- Migrate only the XMR behavior required by this service. Do not copy BTC, exchange, earn, borrow, admin, or upload code unless a current requirement explicitly needs it.
- Prefer extracting a small XMR payment module over copying the old 3,000-line `app/server.py`.
- Use one canonical XMR invoice builder and one canonical confirmation setting.
- Do not copy the old SQLite database into this application by default. Keep the old site available for its unresolved invoices.
- Do not reuse the old service's systemd names, local web port, database path, or onion hidden-service directory.
- Preserve source commit references in migration notes so behavior can be traced.

## Secrets and sensitive data

Never read, print, log, commit, upload, or paste the values of:

- `.env` files or internal tokens
- wallet-rpc usernames or passwords
- Monero wallet files, wallet password files, seeds, or private spend/view keys
- cold-wallet addresses when repository policy treats them as private
- Tor hidden-service private keys
- production SQLite databases or backups
- full deposit addresses, status tokens, or transaction IDs in routine logs and test output

Commit only `.env.example` with names and safe placeholders. Secret files must be outside Git, mode `0600`; wallet and instance directories must be restricted. If a task needs an actual secret or wallet transfer, stop and request an operator action instead of automating it.

## XMR payment invariants

- Represent XMR internally as integer atomic units. Use `Decimal` only at input/output boundaries; never use binary floats for money.
- Create a unique wallet-rpc subaddress for every invoice and persist both account and address indexes.
- Default to 10 confirmations unless the documented deployment decision explicitly changes it.
- Store the exact expected atomic amount at invoice creation; later rate changes must not alter an open invoice.
- The normal lifecycle is:
  `awaiting_payment -> paid_pending_confirmations -> paid_pending_sweep -> sweeping_to_cold -> settled`.
- When sweeping is disabled, a fully paid and confirmed invoice may transition directly to `settled`.
- Only `awaiting_payment` and `paid_pending_confirmations` expire. Never expire a confirmed invoice that is waiting for sweep reconciliation.
- Partial payment must not settle. Overpayment may settle but must preserve the observed amount for reconciliation.
- A failed or uncertain sweep must never grant the service and must never silently issue a duplicate sweep.
- Service fulfillment occurs only after the invoice reaches `settled`.
- Match transfers using both the wallet account index and subaddress index.

## Runtime boundaries

- Bind the web app to loopback only. Tor exposes the selected local port through a separate onion service.
- Bind wallet-rpc to loopback or an explicitly approved private interface. Digest authentication is required.
- Protect any internal polling HTTP endpoint with both loopback-source validation and a strong internal token.
- Keep wallet-rpc lifecycle separate from the web process. If the deployment reuses an existing wallet-rpc, do not install or start a second instance.
- Do not hard-code the legacy install path, port `5000`, database names, usernames, or unit names. Use the deployment decisions and environment-backed configuration.
- Commit sanitized systemd unit templates and operational documentation, not live unit files containing credentials.

## Code structure

Keep responsibilities separated:

- wallet-rpc transport: authentication, retry, timeout, JSON-RPC error handling
- invoice domain: amounts, state transitions, expiry, and idempotency
- persistence: schema and transactional updates
- web routes: validation, CSRF, token-protected checkout/status views
- polling/sweep orchestration: transfer matching, confirmations, sweep, and reconciliation
- deployment: scripts, systemd units, Tor notes, and runbooks

Avoid circular imports and route-local payment logic. Configuration should be loaded once into a typed or clearly structured settings object and validated at startup.

## Testing and verification

Run the narrowest relevant tests during development and the full suite before declaring a task complete. The expected baseline command is:

```bash
python -m pytest
```

Payment tests must use fakes/mocks or a stagenet wallet. Never send mainnet XMR from automated tests.

Required regression coverage includes:

- atomic-unit conversion and input validation
- one unique subaddress per invoice
- partial, exact, and overpayment behavior
- low and sufficient confirmations
- expiry boundaries
- wallet-rpc outage and malformed RPC responses
- sweep success, failure, retry, and response-loss reconciliation
- no duplicate sweep after a stored sweep transaction ID
- localhost plus internal-token enforcement
- token-protected checkout, QR, and status routes
- fulfillment only after `settled`

For systemd changes, validate unit syntax without enabling or starting services unless the user explicitly approves deployment. For production checks, redact sensitive values from command output.

## Definition of done

A task is done only when:

- its acceptance criteria pass;
- relevant tests pass and the commands/results are reported;
- configuration and operational behavior are documented;
- no secret or production data entered Git;
- `git diff` contains only intentional task changes;
- limitations, manual operator steps, and unverified production behavior are clearly stated.

## Code review rules

Flag as blocking:

- floats used for XMR amounts;
- shared deposit addresses;
- settlement before required confirmations or before a required sweep;
- duplicate-sweep windows without reconciliation;
- public wallet-rpc or web binds not explicitly approved;
- credentials in code, unit files, logs, tests, or docs;
- hard-coded paths/ports copied from the legacy repository;
- destructive database, wallet, systemd, or Tor actions without explicit operator approval;
- claims of successful production payment handling based only on mocked tests.
