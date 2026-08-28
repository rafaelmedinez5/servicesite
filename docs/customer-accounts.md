# Customer accounts

Customer accounts provide the identity boundary required before checkout. The
catalog and individual service information remain public; creating an invoice
requires a valid customer session.

## Routes

| Method | Route | Behavior |
| --- | --- | --- |
| `GET, POST` | `/register` | Validate a new username/password and create the account |
| `GET, POST` | `/login` | Verify credentials and start the signed session |
| `GET` | `/account` | Show the current username and checkout readiness |
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

Password recovery, password changes, customer purchase history, and account
deletion are not part of this checkpoint. Customers must store their credentials
in a password manager. The cart/order milestone can add customer-owned order
records without weakening the existing bearer-token payment status boundary.

## Upgrade procedure

Schema version 5 adds `customer_accounts` and `customer_login_guard`. Stop the
web service, take and verify a SQLite online backup, run
`SQLiteDatabase.initialize()` with the production environment loaded, and start
the service only after migration succeeds. The migration does not alter existing
catalog or invoice rows. Follow the exact commands in `deploy-xmr.md`.
