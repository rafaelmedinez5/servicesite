# Checkout requests and delivery

## Customer flow

`Buy this service now` ensures that the selected service is in the saved cart
and redirects to `GET /cart/checkout`. Other cart items remain included. If the
selected service is already there, its quantity and cart revision are preserved.
No rate lookup, wallet call, or invoice creation happens at this step.

The cart's Continue to checkout link opens the same review form. Empty or
unavailable carts return to the cart for correction. Every service line has
one required request textarea (1–4,000 characters); a multi-unit line uses one
request covering all units. The form also requires one delivery method and
contact for the entire order: email or Telegram.

Email validation supports common unquoted ASCII addresses, including plus
addressing and subdomains. International domains can use their ASCII punycode
form. Telegram accepts a standard username of 5–32 ASCII letters, digits or
underscores, starting with a letter, optionally prefixed by `@`. Usernames are
stored with one `@`; email domains are lowercased. Links, phone numbers, spaces,
and malformed contacts are rejected. Validation checks format, not ownership,
existence, or reachability; no verification email or Telegram request is sent.

The form works without JavaScript and uses multipart encoding to accommodate a
full cart of Unicode requests. Checkout bodies are limited to 384 KiB; other
non-upload forms retain their 16 KiB limit. Existing cart size and quantity
limits are unchanged.

On submit, CSRF, nonce, current cart revision, catalog fingerprint, and the
SQLite checkout claim are checked. Requests and contact fields are validated
before quote or wallet calls. Validation errors keep the entered values in the
response, mark the affected fields, release the claim, and issue a fresh nonce.
Only a valid completed review creates the invoice and opens the payment page.

## Storage and privacy

Schema 8 adds two tables without changing existing payment or catalog rows:

- `order_checkout_details`: one delivery method and contact per owned invoice;
- `order_item_requests`: one request per saved invoice/service pair, linked to
  the corresponding immutable invoice item with a foreign key.

Invoice, customer ownership, line snapshots, contact, requests, and cart clearing
are committed in one transaction. A failed save rolls everything back and leaves
the cart available for retry. The existing claim and revision checks continue to
prevent duplicate checkout or stale-cart commits.

Only the owning customer's `/account/orders/<invoice_id>` view and the
authenticated admin purchase-detail view render these fields. The shared
payment/status/QR routes do not expose them. Text is escaped and is never turned
into clickable external contact links. Inputs are not placed in session cookies,
URLs, wallet labels, or logs. Object representations omit contact and requests.
Existing orders without checkout details continue to render normally.

Contacts and requests are stored in the restricted application SQLite database
and will be included in its backups; they are not end-to-end encrypted. Customers
are told not to submit passwords or private keys. Fulfillment remains manual and
requires settled payment plus separate scope/authorization review.

## Required deployment migration: schema 7 → 8

There are no dependency or environment-variable changes. Pulling or restarting
alone does not migrate the database. Follow the verified online-backup procedure
in `deploy-xmr.md`, then stop the web service, pull the merged revision and run
the same initializer with the protected environment loaded:

```bash
systemctl stop servicesite-web.service
runuser -u servicesite -- git -C /opt/servicesite/app pull --ff-only origin main
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && exec /opt/servicesite/.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(\"/etc/servicesite/servicesite.env\"); from app.config import Settings; from app.persistence import SQLiteDatabase; SQLiteDatabase(Settings.from_env().database_path).initialize()"'
```

Stop if any command fails. After successful initialization:

```bash
systemctl start servicesite-web.service
curl -fsS http://127.0.0.1:5100/health
```

The initializer supports recognized earlier schemas and is safe to rerun. Do not
delete or recreate the database or restore an old backup just to change schemas.
If rolling back code to schema 7, preserve the database and new tables; old code
does not use these additive tables, but its older initializer will reject schema
8 and must not be run against it.

## Verification boundary

Tests use temporary databases and fake rates/wallets. Coverage includes buy-now
with empty and existing carts, required per-item requests, email/Telegram format
checks, error retention, maximum-size Unicode forms, CSRF, replay/stale review,
atomic storage rollback, ownership and admin visibility, escaped text, and
repeatable migration of existing orders. No production database, real payment,
delivery message, or browser end-to-end verification is performed.
