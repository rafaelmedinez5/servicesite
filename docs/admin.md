# Administrator workflow

Status: implemented and covered by local regression tests; target-host
configuration and Tor-browser verification remain operator actions.

## Boundary

The first release has one administrator account at `/admin/login`. The panel is
fully server-rendered and contains no JavaScript. It supports:

- creating, editing, publishing, and archiving categories;
- creating, editing, publishing, and archiving USD-priced services;
- purchase filtering by category, service, UTC date, payment state, and
  fulfillment state;
- purchase detail without bearer status tokens, deposit addresses, or deposit/
  sweep transaction identifiers; and
- marking a purchase fulfilled only after its payment reaches `settled`.

Catalog edits never change an existing invoice snapshot. Archival replaces
deletion. Fulfillment is a separate state and cannot settle an invoice or begin
an authorized security engagement by itself.

## Authentication controls

- only a Werkzeug password hash is stored in `/etc/servicesite/servicesite.env`;
- the plaintext password is never stored in Git or shell history;
- sessions expire after `ADMIN_SESSION_HOURS` and are not extended on requests;
- production cookies are Secure, HttpOnly, and SameSite Strict;
- every state-changing form requires the session CSRF token;
- five failed logins within fifteen minutes create a fifteen-minute global
  block recorded in SQLite; and
- every admin response is `no-store`, `noindex`, no-referrer, frame-denied, and
  governed by the no-script content security policy.

The global limit is intentional because Tor forwards onion requests from a
loopback source. It can cause a temporary administrator lockout during a focused
attack; do not weaken it without replacing it with a stronger boundary.

## Generate the external password hash

Run the hash generator locally on the target console. `getpass` prevents the
plaintext from being echoed or placed in shell history:

```bash
runuser -u servicesite -- /opt/servicesite/.venv/bin/python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; first=getpass("New admin password: "); second=getpass("Confirm admin password: "); raise SystemExit("Passwords did not match" if first != second else print(generate_password_hash(first)))'
```

Copy only the resulting `scrypt:...` hash into the protected environment file as
`ADMIN_PASSWORD_HASH`. Do not paste the hash or password into chat. Keep
`ADMIN_USERNAME` at the locally selected value and set `ADMIN_SESSION_HOURS`
between 1 and 24.

## Upgrade an existing schema-version-2 database

1. Keep the payment timer disabled.
2. Take an online SQLite backup and verify `PRAGMA integrity_check` returns
   exactly `ok`.
3. Stop `servicesite-web.service`; wallet-RPC may remain active.
4. Pull the reviewed application revision and install locked dependencies.
5. Add the three admin settings to the protected environment without printing
   the file.
6. Run `SQLiteDatabase.initialize()` using the command in `deploy-xmr.md`.
7. Start the web service, run runtime preflight, and sign in through Tor.

Do not create or fulfill a real purchase until stagenet payment verification is
complete. Keep the polling timer disabled until the Task 8 acceptance matrix is
approved.
