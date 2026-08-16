# servicesite VPS preparation — Ubuntu 24.04 x86_64

This runbook prepares a fresh Shinjiru VPS before application development/deployment.

## Target architecture

```text
Internet / Tor network
        |
        v
   Tor daemon
        |
        | HiddenServicePort -> 127.0.0.1:<APP_PORT>
        v
  Gunicorn / Flask
  user: servicesite
        |
        | authenticated loopback RPC
        v
  monero-wallet-rpc
  user: xmrwallet
        |
        v
   Monero daemon/network
```

The web application must never read the Monero wallet files. The wallet-rpc must never bind to a public interface.

## Target filesystem

```text
/opt/servicesite/
├── app/
├── venv/
├── instance/
├── scripts/
├── logs/
└── secrets/

/opt/monero/
├── wallet/
├── wallet-rpc/
└── logs/

/etc/servicesite/
└── servicesite.env

/var/lib/tor/servicesite/
└── onion-service state and keys
```

Recommended ownership:

- `/opt/servicesite` and its application data: `servicesite:servicesite`
- `/opt/monero/wallet`: `xmrwallet:xmrwallet`
- `/opt/monero/wallet-rpc`: `xmrwallet:xmrwallet`
- `/etc/servicesite/servicesite.env`: `root:servicesite`, mode `0640`, only if the application must read it directly; otherwise use a root-owned secret mechanism appropriate to the service
- Tor state: managed by the distro `debian-tor` account

Do not place wallet files inside `/opt/servicesite`.

## Console-only administration

Routine administration is performed through the Shinjiru HTML5 serial console. Do not configure a public SSH service merely because GitHub is used.

If SSH is later enabled for GitHub repository access or another operational purpose, treat that as a separate decision. Never use a GitHub deploy key as a server-login credential.

## Phase 1 — Confirm the fresh VPS

Run as the initial administrative account/root through the serial console:

```bash
set -euo pipefail

whoami
hostnamectl
uname -m
. /etc/os-release && printf 'Ubuntu: %s %s\n' "$NAME" "$VERSION_ID"

df -hT
free -h
ip -brief address
ss -lntup
```

Expected platform: Ubuntu 24.04 x86_64. If it differs, stop and update the repository decision record before continuing.

Do not paste passwords, private keys, wallet information, or full environment files into chat or GitHub issues.

## Phase 2 — Update the OS and install baseline packages

```bash
apt update
apt full-upgrade -y
apt install -y \
  ca-certificates \
  curl \
  git \
  gnupg \
  jq \
  sqlite3 \
  ufw \
  unattended-upgrades \
  python3 \
  python3-venv \
  python3-pip \
  build-essential \
  tor
```

Reboot if the upgrade requires it:

```bash
systemctl reboot
```

After reconnecting to the serial console, verify:

```bash
systemctl is-system-running || true
systemctl status tor --no-pager
python3 --version
sqlite3 --version
```

Do not install Monero binaries from an unverified third-party package repository. The exact Monero release and verification procedure will be recorded in a later deployment task.

## Phase 3 — Create service identities

Create dedicated non-login service accounts:

```bash
adduser --system --group --home /opt/servicesite servicesite
adduser --system --group --home /opt/monero xmrwallet
```

Verify:

```bash
id servicesite
id xmrwallet
getent passwd servicesite xmrwallet
```

Do not give either account sudo access.

Do not add `servicesite` to `xmrwallet` or vice versa.

## Phase 4 — Create directories and permissions

```bash
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite/app
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite/venv
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite/instance
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite/scripts
install -d -o servicesite -g servicesite -m 0750 /opt/servicesite/logs
install -d -o servicesite -g servicesite -m 0700 /opt/servicesite/secrets

install -d -o xmrwallet -g xmrwallet -m 0700 /opt/monero
install -d -o xmrwallet -g xmrwallet -m 0700 /opt/monero/wallet
install -d -o xmrwallet -g xmrwallet -m 0750 /opt/monero/wallet-rpc
install -d -o xmrwallet -g xmrwallet -m 0750 /opt/monero/logs

install -d -o root -g root -m 0750 /etc/servicesite
```

Check that the application user cannot enter the wallet directory:

```bash
sudo -u servicesite test ! -r /opt/monero/wallet
sudo -u servicesite test ! -x /opt/monero/wallet
```

If either command behaves unexpectedly, stop and fix permissions before installing the wallet.

## Phase 5 — Firewall baseline

Because routine administration is through the serial console and the application is Tor-only, do not blindly open SSH or the Flask port.

First inspect the current firewall state:

```bash
ufw status verbose
```

Then establish a deny-incoming/allow-outgoing baseline only after confirming that no provider-required management path depends on a firewall rule:

```bash
ufw default deny incoming
ufw default allow outgoing
```

Do **not** open the Gunicorn application port or wallet-rpc port. They are loopback-only.

Tor needs outbound connectivity; do not block its outbound connections.

Enable UFW only after reviewing the provider's console/recovery behavior:

```bash
ufw enable
ufw status verbose
```

The serial console must remain usable even if firewall configuration is incorrect.

## Phase 6 — Application Git checkout

The application repository is public/private according to the GitHub repository configuration. Clone it into the new application directory using the GitHub authentication method selected for the development workflow.

Do not store a personal GitHub password or long-lived personal access token in the repository.

Example structure after checkout:

```text
/opt/servicesite/
├── app source from Git
├── venv/          # untracked
├── instance/      # untracked
├── secrets/       # untracked
└── logs/          # untracked
```

Confirm `.gitignore` excludes all runtime and secret data before creating any production configuration.

## Phase 7 — Python environment

After the repository is checked out:

```bash
cd /opt/servicesite
python3 -m venv /opt/servicesite/venv
/opt/servicesite/venv/bin/python -m pip install --upgrade pip
```

Install only dependencies declared by the repository. Do not use `sudo pip`.

The application should eventually run as `servicesite`, not root.

## Phase 8 — Application environment file

Create the production environment file only after the application configuration contract is finalized:

```bash
install -o root -g servicesite -m 0640 /dev/null /etc/servicesite/servicesite.env
```

Populate it manually from the repository's `.env.example` using the operator's private values. Never commit the resulting file.

Do not put wallet seeds, wallet passwords, or Tor private keys in the application repository.

## Phase 9 — Monero wallet-rpc preparation

Do not install or initialize the production wallet until the exact Monero release, daemon topology, wallet filename, RPC port, authentication method, and backup procedure are recorded.

Target properties:

- dedicated `xmrwallet` account
- wallet files under `/opt/monero/wallet`
- wallet-rpc bound to `127.0.0.1` on a dedicated unused port
- authenticated RPC
- no public wallet-rpc listener
- separate systemd unit from Gunicorn
- logs accessible to operators without exposing wallet secrets
- wallet backup procedure tested before accepting production payments

A later task will create the wallet and wallet-rpc service. Do not improvise the wallet initialization command here.

## Phase 10 — Tor onion service preparation

Do not copy the legacy onion directory or private key.

Target mapping:

```text
<generated>.onion:80
        |
        v
127.0.0.1:<APP_PORT>
```

The application port must be selected after checking current listeners:

```bash
ss -lntup
```

Do not bind Flask/Gunicorn to `0.0.0.0`.

A later task will create the dedicated Tor HiddenServiceDir and verify its permissions and generated hostname.

## Phase 11 — Systemd preparation

Do not enable production services yet.

Expected units:

```text
servicesite.service
servicesite-wallet-rpc.service
servicesite-xmr-poll.service
servicesite-xmr-poll.timer
```

If wallet-rpc is supervised by another approved mechanism, record that decision instead of creating a duplicate unit.

Units must:

- run under their dedicated non-root users
- load secrets through an external environment/secret mechanism
- use explicit working directories
- use loopback-only network bindings where applicable
- have bounded restart behavior
- use `NoNewPrivileges=yes` where compatible
- avoid exposing secrets in `ExecStart`
- avoid writing outside intended paths

Validate units with:

```bash
systemd-analyze verify /path/to/unit
```

Do not run `systemctl enable`, `start`, `restart`, or `stop` until the deployment approval gate.

## Phase 12 — Security verification before development

Run:

```bash
ss -lntup
systemctl --type=service --state=running --no-pager
ufw status verbose
find /opt/servicesite /opt/monero -maxdepth 2 -type f -perm /007 -ls
```

Confirm:

- no Flask/Gunicorn public listener
- no wallet-rpc public listener
- no unexpected SSH listener
- application user cannot read wallet files
- wallet user cannot write application source
- runtime directories are outside Git
- Tor is running
- firewall state matches the documented policy

## Development gate

The VPS is considered **READY FOR DEVELOPMENT** only when:

- Ubuntu 24.04 x86_64 is verified
- service users and ownership are correct
- application directory exists
- SQLite/runtime directories exist
- Python venv can be created
- Git checkout/authentication works without embedding secrets
- firewall policy is reviewed
- no application or wallet service is running prematurely
- Tor is installed/running but the new onion service is not yet exposed unless intentionally staged
- Monero release/wallet initialization plan is documented

Do not start accepting real payments until the wallet backup, wallet-rpc, systemd, Tor, invoice reconciliation, and end-to-end payment tests have passed their separate approval gates.
