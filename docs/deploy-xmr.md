# XMR deployment, staging, and rollback runbook

Status: Task 7 operator runbook. It records a new Ubuntu 24.04 x86_64 VPS,
Gunicorn on `127.0.0.1:5100`, a dedicated wallet-RPC on
`127.0.0.1:28088`, a remote Monero daemon, and a new Tor hidden service.
This document does not authorize a live deployment. Commands that mutate the
host are marked **OPERATOR ACTION**.

Run the preflight as `root` on the VPS so it can inspect both identities'
metadata and invoke `runuser`; it still never displays either private file. The
preflight is deliberately read-only. It never creates an invoice, transfers
or sweeps XMR, modifies the database, changes a file, restarts a service, or
prints configuration values. Run it locally on the VPS; share only its sanitized
PASS/FAIL lines.

## Fixed topology and exclusions

| Boundary | Recorded value |
| --- | --- |
| Application root | `/opt/servicesite/app` |
| Python environment | `/opt/servicesite/.venv` |
| SQLite directory | `/opt/servicesite/instance` |
| External app environment | `/etc/servicesite/servicesite.env`, mode `0600` |
| Web identity and listener | `servicesite:servicesite`, `127.0.0.1:5100` |
| Wallet root | `/opt/monero` |
| Wallet identity and listener | `xmrwallet:xmrwallet`, `127.0.0.1:28088` |
| Wallet configuration | `/etc/servicesite/monero-wallet-rpc.conf`, mode `0600` |
| XMR policy | 10 confirmations, two-hour invoice TTL, sweeping disabled initially |
| Daemon | operator-selected remote daemon; no local `monerod` |
| Public route | new Tor hidden service to `127.0.0.1:5100` only |

Redis is not used. Do not install or operate a second wallet-RPC, copy the old
SQLite database, reuse an old onion directory/key, or point this application at
the legacy site's active wallet. The legacy site remains available for its
unresolved invoices.

## OPERATOR APPROVAL gate

Record the person, date, reviewed commit, target host, and outcome for each
approved mutation. Approval is required separately for:

1. repairing the package database and installing OS packages;
2. trusting and installing a verified Monero release;
3. creating external secrets and wallet configuration;
4. creating or manually transferring any wallet material on the new VPS;
5. editing/reloading Tor and exposing the new onion service;
6. copying/reloading/enabling systemd units;
7. initializing or restoring the SQLite database;
8. starting wallet-RPC and the web service;
9. enabling the XMR timer after staging; and
10. enabling sweeping or moving any mainnet XMR.

Task 7 grants none of these approvals. Never paste wallet seeds, keys, RPC
credentials, tokens, full addresses, transaction IDs, onion hostnames, or
environment/configuration file contents into a ticket, chat, shell history, or
Git.

## 1. Read-only collision and host checks

The install preflight expects both selected ports and all four unit names to be
unused. It also checks the recorded paths, owners, private modes, production
application settings, exact Tor mapping, Monero version/options, package audit,
and database-directory access.

```bash
cd /opt/servicesite/app
scripts/preflight --phase install
```

It must fail until all prerequisites below exist. Failure is a stop condition,
not permission to weaken a check. For manual corroboration, these commands are
read-only and display no secret file contents:

```bash
dpkg --audit
ss -ltnp '( sport = :5100 or sport = :28088 )'
systemctl show --property=LoadState --value servicesite-wallet-rpc.service
systemctl show --property=LoadState --value servicesite-web.service
systemctl show --property=LoadState --value servicesite-poll-xmr.service
systemctl show --property=LoadState --value servicesite-poll-xmr.timer
find /opt/servicesite /opt/monero -maxdepth 2 -printf '%M %u:%g %p\n'
```

An existing listener, unit, database, wallet file, or hidden-service directory
must be identified before proceeding. On a same-VPS deployment, compare it with
the legacy service and choose new names/paths instead of overwriting anything.
On the recorded new VPS, an unexpected collision is still a blocker.

## 2. OS and Python prerequisites

The host previously reported an incomplete package operation involving
`tzdata`. Repair it before package installation.

**OPERATOR ACTION — package database and OS packages:**

```bash
dpkg --audit
dpkg --configure -a
apt-get -f install
dpkg --audit
apt-get update
apt-get install ca-certificates curl git gnupg bzip2 python3 python3-venv sqlite3 tor
```

Stop if the final audit is not empty. Do not continue around an interrupted
`dpkg` state.

The repository and virtual environment are already expected at the recorded
paths. To refresh only after reviewing the selected Git commit:

**OPERATOR ACTION — application checkout and dependencies:**

```bash
runuser -u servicesite -- git -C /opt/servicesite/app pull --ff-only
runuser -u servicesite -- /opt/servicesite/.venv/bin/pip install -r /opt/servicesite/app/requirements.txt
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && /opt/servicesite/.venv/bin/python -m pytest'
```

Do not proceed unless the full suite passes.

## 3. Ownership and private directories

Application code and its virtual environment remain owned by the application
identity so the reviewed checkout can be updated through that identity. Wallet
binaries are immutable to `xmrwallet`: root owns `/opt/monero` and its `bin`
directory, while only wallet state and wallet logs are writable by `xmrwallet`.

**OPERATOR ACTION — create or correct directories:**

```bash
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite /opt/servicesite/app /opt/servicesite/logs
install -d -o servicesite -g servicesite -m 0700 /opt/servicesite/instance /opt/servicesite/secrets
install -d -o root -g xmrwallet -m 0750 /opt/monero /opt/monero/bin
install -d -o xmrwallet -g xmrwallet -m 0750 /opt/monero/logs
install -d -o xmrwallet -g xmrwallet -m 0700 /opt/monero/wallet
install -d -o root -g root -m 0711 /etc/servicesite
```

The execute-only access on `/etc/servicesite` lets each service open its known,
separately owned `0600` file without allowing directory listing. Do not use
recursive ownership changes after wallet or secret files exist.

## 4. Verify and install Monero binaries

Task 7 pins the official Linux x64 release `v0.18.5.1`. The archive is
`monero-linux-x64-v0.18.5.1.tar.bz2` with SHA-256
`22a7dda7b0cb699fdd6b7674c3b4a4465b337cc98a54983523b759e1e7cc9958`.
The signed hashes file remains the canonical source; the pinned value is an
additional comparison, not a replacement for signature verification.

Perform verification in a new operator-owned temporary directory before
extraction. The expected binaryFate signing-key fingerprint is:

```text
81AC 591F E9C4 B65C 5806 AFC3 F0AF 4D46 2A0B DF92
```

**OPERATOR ACTION — download and verify before extraction:**

```bash
umask 077
MONERO_ARCHIVE=monero-linux-x64-v0.18.5.1.tar.bz2
MONERO_SHA256=22a7dda7b0cb699fdd6b7674c3b4a4465b337cc98a54983523b759e1e7cc9958
curl -fSLo binaryfate.asc https://raw.githubusercontent.com/monero-project/monero/master/utils/gpg_keys/binaryfate.asc
gpg --import binaryfate.asc
gpg --fingerprint F0AF4D462A0BDF92
curl -fSLO https://www.getmonero.org/downloads/hashes.txt
gpg --verify hashes.txt
curl -fSLO "https://downloads.getmonero.org/cli/${MONERO_ARCHIVE}"
printf '%s  %s\n' "$MONERO_SHA256" "$MONERO_ARCHIVE" | sha256sum --check --strict -
```

Visually compare the entire fingerprint and stop on any signature, fingerprint,
or hash mismatch. Only then inspect and extract the archive.

**OPERATOR ACTION — install only the reviewed wallet tools:**

```bash
tar -tjf "$MONERO_ARCHIVE"
tar -xjf "$MONERO_ARCHIVE"
install -o root -g xmrwallet -m 0750 monero-x86_64-linux-gnu-v0.18.5.1/monero-wallet-rpc /opt/monero/bin/monero-wallet-rpc
install -o root -g xmrwallet -m 0750 monero-x86_64-linux-gnu-v0.18.5.1/monero-wallet-cli /opt/monero/bin/monero-wallet-cli
/opt/monero/bin/monero-wallet-rpc --version
/opt/monero/bin/monero-wallet-rpc --help >/dev/null
```

Do not install or start `monerod` for this topology. If the archive layout or
reported version differs, stop and re-review the release instead of altering the
preflight expectation.

Official references:

- [Monero release v0.18.5.1](https://github.com/monero-project/monero/releases/tag/v0.18.5.1)
- [Monero advanced binary verification](https://www.getmonero.org/resources/user-guides/verification-allos-advanced.html)
- [monero-wallet-rpc reference](https://docs.getmonero.org/interacting/monero-wallet-rpc-reference/)

## 5. External application environment

Use `.env.example` as a list of names, not as production values. Generate every
secret locally with a cryptographically secure tool, enter it through a local
root editor, and keep the result out of Git and terminal output. The RPC username
and password must exactly match the wallet-RPC configuration. The initial file
must select:

- `ENVIRONMENT=production`;
- `APP_HOST=127.0.0.1` and `APP_PORT=5100`;
- `DB_PATH=/opt/servicesite/instance/servicesite.db`;
- the selected `ADMIN_USERNAME` and an `ADMIN_SESSION_HOURS` value from 1
  through 24; the first password is created after migration through the
  one-time admin setup page and its hash is stored in SQLite; an optional
  `ADMIN_RECOVERY_PIN` must contain exactly six ASCII digits when enabled;
- `XMR_WALLET_RPC_URL=http://127.0.0.1:28088/json_rpc`;
- 10 confirmations and a 7,200-second invoice TTL;
- `XMR_SWEEP_ENABLED=false`; and
- `ALLOW_PUBLIC_XMR_WALLET_RPC=false`.

**OPERATOR ACTION — create the file without printing it:**

```bash
umask 077
install -o servicesite -g servicesite -m 0600 /opt/servicesite/app/.env.example /etc/servicesite/servicesite.env
editor /etc/servicesite/servicesite.env
chown servicesite:servicesite /etc/servicesite/servicesite.env
chmod 0600 /etc/servicesite/servicesite.env
stat -c '%a %U:%G %n' /etc/servicesite/servicesite.env
```

Never use the example placeholders in production. Do not run `cat`, `grep`,
`env`, `set`, or shell tracing against the completed file.

## 6. Wallet, daemon, and wallet-RPC configuration

Use a fresh stagenet wallet for the STAGING gate. Any mainnet wallet creation,
restoration, or transfer is an interactive operator procedure on the new VPS.
Never automate or record seed/private-key extraction. Never copy an actively
used legacy wallet while the old site has unresolved invoices. Never transfer
funds merely because a mocked test passed.

The operator selects and validates a remote daemon. Prefer an onion daemon. A
clearnet daemon may be used only through the local Tor SOCKS proxy after the
installed wallet binary confirms the proxy option. Treat the daemon as
untrusted. Record the selection privately; do not commit or paste it.

Create `/etc/servicesite/monero-wallet-rpc.conf` with a local root editor. After
checking `--help`, configure these keys locally:

| Key | Requirement |
| --- | --- |
| `rpc-bind-ip` / `rpc-bind-port` | `127.0.0.1` / `28088` |
| `rpc-login` | strong local digest username and password matching the app environment |
| `wallet-file` / `password-file` | files below `/opt/monero/wallet`, both private to `xmrwallet` |
| `daemon-address` | privately selected remote daemon, never a public wallet-RPC bind |
| `proxy` | local Tor SOCKS listener when required by the selected daemon route |
| `untrusted-daemon` | enabled |
| `stagenet` | enabled for STAGING; removed only at the PRODUCTION gate |
| `log-file` | protected file below `/opt/monero/logs` |

**OPERATOR ACTION — create and protect wallet configuration:**

```bash
umask 077
editor /etc/servicesite/monero-wallet-rpc.conf
chown xmrwallet:xmrwallet /etc/servicesite/monero-wallet-rpc.conf
chmod 0600 /etc/servicesite/monero-wallet-rpc.conf
stat -c '%a %U:%G %n' /etc/servicesite/monero-wallet-rpc.conf
```

Create the stagenet wallet through an interactive, local
`monero-wallet-cli --stagenet` session running as `xmrwallet`. Do not capture or
share its transcript. The service must start wallet-RPC with HTTP Digest auth,
loopback bind, an untrusted remote daemon, and no externally reachable RPC
interface.

## 7. Fresh SQLite initialization and backup

Do not copy the legacy database. Before initialization, the selected database
path must not exist unless it is already a recognized servicesite database.

**OPERATOR ACTION — initialize the fresh schema:**

```bash
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && exec /opt/servicesite/.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(\"/etc/servicesite/servicesite.env\"); from app.config import Settings; from app.persistence import SQLiteDatabase; SQLiteDatabase(Settings.from_env().database_path).initialize()"'
stat -c '%a %U:%G %n' /opt/servicesite/instance/servicesite.db
```

The database must be mode `0600`, owned by `servicesite`, and use the current
schema. Stop if initialization reports existing unrecognized tables.

After deploying a revision with a newer recognized schema, stop the web service,
take and verify an online backup, then rerun the same initialization command.
The recognized migration preserves existing invoices. Start the web service
only after the command succeeds; never edit schema tables manually.

Create backups with SQLite's online backup operation; never copy a live WAL
database with plain `cp`.

**OPERATOR ACTION — create a restricted backup with an explicit unique name:**

```bash
install -d -o servicesite -g servicesite -m 0700 /opt/servicesite/instance/backups
runuser -u servicesite -- sqlite3 /opt/servicesite/instance/servicesite.db ".backup /opt/servicesite/instance/backups/servicesite-YYYYMMDD-HHMMSS.db"
runuser -u servicesite -- sqlite3 /opt/servicesite/instance/backups/servicesite-YYYYMMDD-HHMMSS.db 'PRAGMA integrity_check;'
chmod 0600 /opt/servicesite/instance/backups/servicesite-YYYYMMDD-HHMMSS.db
```

Expect exactly `ok` from the integrity check. Store an encrypted off-host copy
under the operator's backup policy. A backup is incomplete without a tested
restore.

**OPERATOR ACTION — restore is destructive to the active selection and requires
its own approval:** stop the timer first, stop the web process, verify the chosen
backup with `PRAGMA integrity_check`, restore it into a new `0600`
`servicesite.restore.db`, and verify that file. Move the current database and
any explicit `-wal`/`-shm` sidecars to unique `pre-restore` names; do not delete
them. Move the verified restore into place, confirm ownership/mode, then run the
runtime preflight before restarting. Never restore into the legacy site's path.

## 8. New Tor hidden service

Create a new hidden-service identity. Do not copy the old hidden-service
directory, hostname, or private keys. Add these two directives once to the
target host's `/etc/tor/torrc`:

```text
HiddenServiceDir /var/lib/tor/servicesite/
HiddenServicePort 80 127.0.0.1:5100
```

**OPERATOR ACTION — edit, verify, and reload Tor:**

```bash
editor /etc/tor/torrc
tor --verify-config -f /etc/tor/torrc
systemctl reload tor.service
stat -c '%a %U:%G %n' /var/lib/tor/servicesite
```

The hidden-service directory must be owned by Tor and mode `0700`. Inspect the
new onion hostname locally; do not paste it into routine logs or chat. Tor setup
reference: [official onion-service setup](https://community.torproject.org/onion-services/setup/).

## 9. Systemd order and sanitized observation

Run the install preflight again. It must pass before units are copied:

```bash
cd /opt/servicesite/app
scripts/preflight --phase install
scripts/verify-systemd
```

Then follow `docs/systemd-install.md`. The required order is:

1. copy all four reviewed units and run `systemctl daemon-reload`;
2. start `servicesite-wallet-rpc.service`;
3. start `servicesite-web.service`;
4. run `scripts/preflight --phase runtime`;
5. complete STAGING stagenet payment/reconciliation tests in Task 8; and
6. only after approval, enable `servicesite-poll-xmr.timer`.

The runtime preflight performs only TCP connection checks, fixed web `/health`,
authenticated wallet `get_height`, file comparisons, and read-only systemd
status queries. It never runs the poll endpoint.

Use only sanitized observation commands:

```bash
curl -fsS http://127.0.0.1:5100/health
systemctl is-active servicesite-wallet-rpc.service servicesite-web.service
systemctl status servicesite-poll-xmr.timer --no-pager
systemctl list-timers servicesite-poll-xmr.timer --no-pager
journalctl -u servicesite-web.service --since today --lines 100 --no-pager
journalctl -u servicesite-poll-xmr.service -u servicesite-poll-xmr.timer --since today --lines 100 --no-pager
```

Do not share raw wallet logs. Review all output locally and redact addresses,
tokens, transaction IDs, onion names, daemon details, and customer data before
sharing an excerpt.

## STAGING gate

STAGING uses a fresh stagenet wallet, sweeping disabled, the selected remote
stagenet daemon route, and the timer still disabled. Before Task 8 begins, all of
the following must be true:

- repository tests and both systemd syntax checks pass;
- `scripts/preflight --phase install` passed immediately before installation;
- only the reviewed units were installed;
- wallet-RPC and web services are active on loopback only;
- `scripts/preflight --phase runtime` passes;
- the database is fresh and backed up;
- the new Tor mapping is exact; and
- no legacy service, database, wallet, or onion state was modified.

Task 8 owns the real stagenet partial/exact/overpayment, confirmation, expiry,
outage, and restart/recovery evidence. A mocked suite cannot satisfy this gate.

## PRODUCTION gate

Do not enter production from Task 7. Production requires completed Task 8
evidence, review of every open/stuck staging invoice, an explicit cutover plan,
a fresh production backup, and separate operator approval. Before mainnet:

- replace the stagenet wallet/configuration through the manual wallet procedure;
- keep wallet-RPC loopback/digest authentication and the remote daemon untrusted;
- verify the new mainnet wallet has no connection to unresolved legacy invoices;
- keep `XMR_SWEEP_ENABLED=false` unless sweep testing and reconciliation were
  separately approved;
- repeat install/runtime preflight at the applicable boundary; and
- enable the timer only after wallet, web, database, Tor, and fulfillment checks
  all pass.

Never grant a service before its invoice reaches `settled`.

## ROLLBACK gate

Rollback protects money and evidence; it does not erase the failed deployment.
Stop new reconciliation first, then preserve everything needed for recovery.

**OPERATOR ACTION — rollback services:**

```bash
systemctl disable --now servicesite-poll-xmr.timer
systemctl stop servicesite-poll-xmr.service
systemctl disable --now servicesite-web.service
systemctl disable --now servicesite-wallet-rpc.service
```

Before stopping wallet-RPC, confirm no approved sweep or reconciliation request
is active. Take a SQLite online backup and record sanitized unit status. Preserve
the new database, WAL evidence, wallet, wallet password file, external configs,
logs, verified binaries, backups, and new onion directory. Do not delete or
overwrite any uncertain sweep attempt or transaction evidence.

The legacy site remains untouched and continues handling its own unresolved
invoices. Rolling back servicesite never authorizes redirecting its invoices to
the legacy wallet, importing its database into the old application, or reusing
the old onion key. Root-cause review and an explicit recovery approval are
required before another deployment attempt.
