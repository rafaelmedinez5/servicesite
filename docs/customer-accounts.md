# Customer accounts

Customer accounts provide the identity boundary for catalog access and
checkout. Anonymous visitors are redirected to login before the catalog or an
individual service can be viewed; creating an invoice requires a valid customer
session.

## Routes

| Method | Route | Behavior |
| --- | --- | --- |
| `GET, POST` | `/register` | Validate a new username/password and create the account |
| `GET, POST` | `/login` | Verify credentials and start the signed session |
| `GET` | `/account` | Show the current username and checkout readiness |
| `GET` | `/account/orders/<invoice_id>` | Show an order belonging to the signed-in customer |
| `POST` | `/logout` | Verify CSRF and clear the signed session |

Usernames are normalized to lowercase and must contain 3 through 32 ASCII
letters, numbers, periods, underscores, or hyphens. They must start and end with
a letter or number and are unique without regard to case. Passwords require at
least 12 characters and are stored only as Werkzeug-generated scrypt or PBKDF2
hashes.

## Security behavior

- Registration, login, and logout submissions require the signed-session CSRF
  token.
- Successful registration or login clears prior session state before storing
  the opaque customer ID, username, and credential version.
- Account state is reloaded from SQLite on each signed-in request; a missing or
  version-mismatched account invalidates the session.
- Five failed password checks for one existing account within fifteen minutes
  block that account's login for fifteen minutes. Unknown usernames use a dummy
  password-hash check and the same generic invalid-credentials response.
- Auth and account responses are `no-store`, private, and `noindex`; all use the
  application's no-script, no-referrer, and frame-denial policy.
- Redirects after login accept only local absolute paths. External or malformed
  destinations are discarded.

The account page lists the customer's 100 most recent orders. Both direct
single-service checkout and cart checkout save ownership together with the
invoice. Earlier invoices are not assigned to accounts retroactively because
their original owner cannot be inferred safely; their bearer links still work
for a signed-in site session.

Password recovery, password changes, and account deletion are not part of this
checkpoint. Customers must store their credentials in a password manager.

## Upgrade procedure

Schema version 5 added `customer_accounts` and `customer_login_guard`. Version 6
adds saved carts, checkout claims, order ownership, and line-item snapshots. Stop the
web service, take and verify a SQLite online backup, run
`SQLiteDatabase.initialize()` with the production environment loaded, and start
the service only after migration succeeds. The migration does not alter existing
catalog or invoice rows. Follow the exact commands in `deploy-xmr.md`.
