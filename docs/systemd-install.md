# Systemd installation and rollback

Status: Task 6 templates are committed and locally syntax-checked. No unit was
installed, enabled, started, restarted, or stopped.

## Prerequisites and boundary

The selected topology uses a dedicated wallet-RPC process. Its binary is not yet
installed at `/opt/monero/bin/monero-wallet-rpc`, so wallet runtime validation is
**BLOCKED until Task 7**. Task 7 must also create the protected external wallet
configuration only after checking the installed binary's `--version` and
`--help`. The only wallet command-line option in the unit is the officially
documented `--config-file` path.

Before any installation, an operator must verify:

- `/opt/servicesite/app` is the reviewed source checkout;
- `/opt/servicesite/.venv` contains the locked Python dependencies;
- `/etc/servicesite/servicesite.env` exists, is owned appropriately, and has
  mode `0600`, without displaying its contents;
- `/etc/servicesite/monero-wallet-rpc.conf` exists, is readable only by
  `xmrwallet`, and has mode `0600`, without displaying its contents;
- the app configuration selects `127.0.0.1:5100` and the wallet configuration
  selects the matching loopback endpoint expected by the application;
- `/opt/servicesite/instance` and `/opt/servicesite/logs` are writable only by
  `servicesite`, while `/opt/monero/wallet` and `/opt/monero/logs` are writable
  only by `xmrwallet`;
- stagenet verification and explicit sweep approval have not been bypassed.

Do not put a wallet password, RPC login, daemon endpoint, internal token, cold
address, wallet filename, or Tor key in a unit file or shell command.

## Repository validation (read-only)

These commands inspect repository files and do not mutate the host:

```bash
scripts/verify-systemd
python -m pytest
```

The helper validates unit syntax against the local systemd parser while replacing
target-host executable paths only in temporary copies. After Task 7 installs all
reviewed files on the target, run direct `systemd-analyze verify` there before
copying units. A missing wallet binary is an expected runtime blocker before
Task 7; it is not permission to download, install, or start an unreviewed binary.

## Install units

**OPERATOR ACTION — mutates `/etc/systemd/system`:**

```bash
install -o root -g root -m 0644 deploy/systemd/servicesite-wallet-rpc.service /etc/systemd/system/servicesite-wallet-rpc.service
install -o root -g root -m 0644 deploy/systemd/servicesite-web.service /etc/systemd/system/servicesite-web.service
install -o root -g root -m 0644 deploy/systemd/servicesite-poll-xmr.service /etc/systemd/system/servicesite-poll-xmr.service
install -o root -g root -m 0644 deploy/systemd/servicesite-poll-xmr.timer /etc/systemd/system/servicesite-poll-xmr.timer
systemctl daemon-reload
```

Do not perform this section until Task 7 preflight passes.

## Enable and start

**OPERATOR ACTION — starts processes and enables boot persistence:**

```bash
systemctl enable --now servicesite-wallet-rpc.service
systemctl enable --now servicesite-web.service
systemctl enable --now servicesite-poll-xmr.timer
```

The timer is enabled only after wallet-RPC and the web health check pass. A
manual start of `servicesite-poll-xmr.service` is a payment-state mutation and is
reserved for an approved staging or production procedure.

## Status, timer, and logs

These commands do not display environment-file contents:

```bash
systemctl status servicesite-wallet-rpc.service servicesite-web.service --no-pager
systemctl status servicesite-poll-xmr.timer --no-pager
systemctl list-timers servicesite-poll-xmr.timer --no-pager
journalctl -u servicesite-web.service --since today --no-pager
journalctl -u servicesite-poll-xmr.service -u servicesite-poll-xmr.timer --since today --no-pager
```

The Gunicorn configuration intentionally disables access logs because private
checkout and status tokens are part of request paths. Application and poll logs
must remain sanitized. Wallet standard output is discarded; its protected log
file and error journal may contain payment metadata and must be inspected locally
and redacted before any excerpt is shared.

## Rollback

**OPERATOR ACTION — stops the new payment poller and application:**

```bash
systemctl disable --now servicesite-poll-xmr.timer
systemctl stop servicesite-poll-xmr.service
systemctl disable --now servicesite-web.service
systemctl disable --now servicesite-wallet-rpc.service
```

Stopping the dedicated wallet-RPC requires checking that no approved recovery or
reconciliation operation is active. Rollback does not delete the fresh database,
wallet, environment files, logs, backups, or Tor hidden-service state.

## Uninstall unit definitions

**OPERATOR ACTION — removes only the four new unit definitions:**

```bash
systemctl disable --now servicesite-poll-xmr.timer servicesite-web.service servicesite-wallet-rpc.service
rm /etc/systemd/system/servicesite-poll-xmr.timer
rm /etc/systemd/system/servicesite-poll-xmr.service
rm /etc/systemd/system/servicesite-web.service
rm /etc/systemd/system/servicesite-wallet-rpc.service
systemctl daemon-reload
systemctl reset-failed
```

This does not authorize deletion of `/opt/servicesite`, `/opt/monero`,
`/etc/servicesite`, any SQLite database, wallet, backup, or onion key.
