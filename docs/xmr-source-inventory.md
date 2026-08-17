# XMR source inventory

Status: source code verified; legacy runtime inventory blocked because the legacy
VPS is unavailable.

This document is deliberately sanitized for a public repository. The private
source repository and pinned source commit were supplied and inspected in the
private migration session. They are represented here as
`<PINNED_PRIVATE_SOURCE_COMMIT>` in accordance with repository policy.

## Evidence labels

- **VERIFIED-SOURCE** — observed in the pinned source code or its tests.
- **BLOCKED-LIVE** — requires read-only access to the legacy VPS and was not
  verified.
- **NEW-DEPLOYMENT** — an approved replacement value for `servicesite`; it is
  not a claim about the legacy host.

No wallet, RPC, token, transaction, onion-key, database, or environment-secret
values were accessed for this inventory.

## Audited source

Private source ref: `<PINNED_PRIVATE_SOURCE_COMMIT>`

The following files were inspected read-only:

- `app/payments/xmr_wallet_rpc.py`
- the XMR invoice, checkout, status, polling, and sweep sections of
  `app/server.py`
- the XMR schema and update helpers in `app/db.py`
- `app/core/pricing.py`
- `scripts/poll-xmr.sh`
- `tests/test_xmr_sweep_status.py`
- the README XMR, configuration, and deployment sections
- `docs/xmr_sweep_note.md`

## Verified source topology

The source describes these separate responsibilities:

1. Tor maps an onion service to the Flask/Gunicorn loopback listener.
2. The web application creates one wallet-RPC subaddress per XMR invoice and
   stores the invoice plus wallet account/address indexes in SQLite.
3. A shell poller loads runtime configuration and sends an authenticated POST to
   the application's loopback-only internal XMR polling endpoint.
4. The polling endpoint reads incoming transfers from wallet-RPC, updates invoice
   observations, enforces confirmations, and optionally sweeps confirmed funds.
5. `monero-wallet-rpc` is operationally separate from Flask. The source does not
   contain its live service definition, wallet path, daemon endpoint, or version.

The source repository does not contain systemd service/timer unit files or a Tor
service definition. Therefore, source code proves the logical relationships but
does not prove the live process users, unit names, timer schedule, environment
file, wallet-RPC flags, or onion mapping.

## Source behavior inventory

### Wallet-RPC transport — VERIFIED-SOURCE

The wallet client:

- uses JSON-RPC over HTTP with `requests.Session`;
- attaches HTTP Digest authentication when a username or password is present;
- applies a configured timeout, bounded attempt count, and linear retry delay;
- exposes subaddress creation, wallet height, incoming transfer lookup,
  transfer-by-transaction lookup, and `sweep_all`;
- defaults its endpoint to a loopback wallet-RPC JSON-RPC URL;
- returns account and address indexes with every newly created subaddress.

Migration caveats:

- the source conversion helper rounds down without fully validating negative,
  excessive-precision, or malformed input;
- broad exceptions are collapsed into one reachability error;
- successful HTTP responses are not schema-validated strongly;
- RPC settings are loaded directly from the environment inside the client;
- digest authentication is optional in the source even though it is mandatory
  for the new deployment.

Task 2 must preserve the useful behavior while adding strict validation,
injected test dependencies, typed settings, and clearer failure categories.

### Invoice creation and persistence — VERIFIED-SOURCE

- Each XMR invoice requests a new subaddress and persists both wallet account and
  subaddress indexes.
- Expected and observed XMR amounts are stored as integer atomic units.
- The database stores required/observed confirmations, deposit transaction ID,
  sweep transaction ID, expiry, and settlement timestamps.
- The source defaults to 10 confirmations and a two-hour invoice lifetime.
- Only `awaiting_payment` and `paid_pending_confirmations` are expired. Confirmed
  sweep-related states are preserved for reconciliation.
- Invoice-building logic exists in both `app/server.py` and
  `app/core/pricing.py`; the new application must use one canonical builder.
- Some legacy pricing paths use binary floats and `math.ceil`. Those paths are
  not suitable for the new application's money boundary and will not be copied.

### Polling and state transitions — VERIFIED-SOURCE

The internal poll route requires both a loopback source address and an internal
header token. The source lifecycle is:

`awaiting_payment -> paid_pending_confirmations -> paid_pending_sweep -> sweeping_to_cold -> settled`

When sweeping is disabled, a fully paid invoice with sufficient confirmations
may transition directly to `settled`. When sweeping is enabled, the poller calls
`sweep_all` for the invoice subaddress, stores the returned sweep transaction ID,
and settles only after a transaction ID is returned. A normal RPC exception
returns the invoice to `paid_pending_sweep`.

Observed limitations that must not be copied:

- incoming transfers are fetched for one configured account and grouped chiefly
  by the subaddress minor index; the new implementation must match the persisted
  account index and subaddress index together;
- there is no demonstrated per-invoice lock protecting overlapping poll runs;
- if a sweep is broadcast but its RPC response is lost before the transaction ID
  is stored, the next poll can issue another sweep;
- the poll endpoint can return a raw exception string in a degraded response;
  production responses and logs must be sanitized;
- one configured confirmation value is read during polling while invoice rows
  also store a required value; the new application must define one canonical
  setting and lock it into each invoice.

### Source regression coverage — VERIFIED-SOURCE

The source sweep tests prove:

- confirmed payment enters a sweep-pending state;
- an ordinary sweep failure does not settle the invoice;
- a later poll retries an ordinary failure;
- an already stored sweep transaction ID prevents another `sweep_all` call.

They do not prove concurrent-poll safety or broadcast-success/response-loss
reconciliation. Both cases remain release-blocking tests for Task 5.

## Hard-coded legacy values that must change

| Source value or pattern | Where observed | Required `servicesite` treatment |
| --- | --- | --- |
| `<LEGACY_INSTALL_DIR>` | poll shell script | Use deployment configuration; approved root is `/opt/servicesite` |
| Application-local `.env` loading | poll shell script | Use `/etc/servicesite/servicesite.env`, mode `0600` |
| `127.0.0.1:5000` | poll script, README, development fallback | Use the approved loopback bind `127.0.0.1:5100` |
| Legacy database and unit names | live host only | Never infer or reuse; create fresh `servicesite` names and database |
| Legacy onion state directory | live host only | Never infer or copy; create a new Tor hidden-service directory |
| Direct environment access throughout payment code | wallet client and routes | Load and validate one application settings object |

## Legacy runtime inventory — BLOCKED-LIVE

The legacy VPS cannot currently be accessed. None of the following may be
treated as known:

| Required fact | Status |
| --- | --- |
| Flask/Gunicorn systemd unit name and effective definition | BLOCKED-LIVE |
| Web service user/group and working directory | BLOCKED-LIVE |
| Effective environment-file path | BLOCKED-LIVE |
| Polling oneshot unit and timer names | BLOCKED-LIVE |
| Polling timer cadence, persistence, and last/next run | BLOCKED-LIVE |
| wallet-RPC systemd unit, user/group, binary version, and flags | BLOCKED-LIVE |
| wallet-RPC live bind and authentication enforcement | BLOCKED-LIVE |
| Monero daemon topology (local, public, or onion) | BLOCKED-LIVE |
| Tor service mapping and hidden-service directory | BLOCKED-LIVE |
| Legacy SQLite path, owner/group, and permissions | BLOCKED-LIVE |
| Current service health, journals, open invoices, and wallet balance | BLOCKED-LIVE |

No live unit contents will be reconstructed from memory. The new deployment will
use independently reviewed, sanitized templates rather than copied legacy units.

## Redacted recovery checklist

If read-only console access or a host backup becomes available later, an operator
may collect only the following metadata. Replace angle-bracket placeholders
locally. Do not paste output containing secrets into chat or Git.

```bash
systemctl list-units --all --type=service --type=timer --no-pager

systemctl show <WEB_UNIT> \
  -p Id -p LoadState -p ActiveState -p SubState -p UnitFileState \
  -p User -p Group -p WorkingDirectory -p FragmentPath

systemctl show <POLL_SERVICE_UNIT> \
  -p Id -p LoadState -p ActiveState -p SubState -p UnitFileState \
  -p User -p Group -p WorkingDirectory -p FragmentPath

systemctl show <POLL_TIMER_UNIT> \
  -p Id -p LoadState -p ActiveState -p SubState -p UnitFileState \
  -p OnBootUSec -p OnUnitActiveUSec -p AccuracyUSec -p Persistent \
  -p LastTriggerUSec -p NextElapseUSecRealtime

systemctl show <WALLET_RPC_UNIT> \
  -p Id -p LoadState -p ActiveState -p SubState -p UnitFileState \
  -p User -p Group -p WorkingDirectory -p FragmentPath

ss -lnt
stat -c '%A %U:%G %n' <LEGACY_APP_DIR> <LEGACY_INSTANCE_DIR> <LEGACY_DB_PATH>
curl -fsS http://127.0.0.1:5000/health
```

For wallet-RPC version and supported flags, invoke the locally installed binary
with `--version` and `--help` only. Do not copy its running command line: command
arguments may contain RPC credentials, wallet paths, or daemon details.

For Tor, inspect only `HiddenServiceDir` and `HiddenServicePort` directives after
locally removing comments and any unrelated proxy/authentication settings. Never
read, archive, or paste files inside a hidden-service state directory.

Do not run `systemctl cat`, `systemctl show -p Environment`, `ps` with full
arguments, `cat` on an environment file, SQLite queries, or any wallet-RPC method
as part of this checklist. Those can expose secrets or mutate/inspect protected
payment data.

## Health checks

- **VERIFIED-SOURCE:** the application defines `GET /health` returning `OK`.
- **VERIFIED-SOURCE:** the old documented local fallback is port `5000`.
- **BLOCKED-LIVE:** whether the old web process is running or actually uses that
  port cannot be confirmed.
- **BLOCKED-LIVE:** timer execution and wallet-RPC health cannot be confirmed.

The internal poll endpoint is not a general health check. It requires the secret
internal token and can update invoices or trigger a sweep, so it must not be used
during read-only inventory.

## Consequences of the unavailable legacy host

- The new site must use a fresh database and a fresh wallet-RPC wallet.
- Old invoice addresses, states, balances, and sweep history will not be imported
  or guessed.
- Any payment sent to an old invoice remains outside the new application's view.
- If a legitimate wallet file or seed backup exists, recovery is a separate
  manual operator procedure on an isolated wallet instance. Never provide the
  seed, keys, or password to an agent or commit them to Git.
- The new service must not accept production payments until its new wallet,
  backup, poller, confirmations, and reconciliation behavior pass staged
  end-to-end verification.

## Source contradictions and open risks

1. The source README describes XMR sweeping as implemented, while
   `docs/xmr_sweep_note.md` describes it as future work. Code and tests confirm
   ordinary sweep support; the note is stale.
2. The source README documents many XMR environment settings, but its committed
   `.env.example` does not provide the XMR configuration contract.
3. The source claims reusable invoice behavior, but XMR invoice construction is
   duplicated and mixed with unrelated business flows.
4. Stored sweep transaction IDs prevent a known duplicate, but uncertain
   broadcast reconciliation is not implemented safely.
5. All live operational facts remain blocked; source defaults must not be
   mistaken for effective production configuration.

## Task 1 conclusion

The reusable source behavior is sufficiently understood to begin Task 2's
isolated wallet-RPC transport and configuration contract. Deployment recreation,
wallet creation, systemd/Tor installation, and production payment acceptance are
not authorized or verified by this inventory.
