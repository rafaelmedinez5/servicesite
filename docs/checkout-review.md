# Checkout requests and delivery

## Customer flow

`Buy this service now` ensures that the selected service is in the saved cart
and redirects to `GET /cart/checkout`. Other cart items remain included. If the
selected service is already there, its quantity and cart revision are preserved.
No rate lookup, wallet call, or invoice creation happens at this step.

The cart's Continue to checkout link opens the same review form. Empty or
unavailable carts return to the cart for correction. Every service line has
one optional request textarea (up to 4,000 characters); a multi-unit line uses
one request covering all units. Missing or whitespace-only requests are saved
as blank. The placeholder suggests including a public PGP key, and a link to
Our PGP key lets customers encrypt a request themselves before pasting it.
The form does not perform encryption or parse/verify submitted PGP keys.

The form requires one delivery method for the entire order: My account, email
or Telegram. My account needs no external contact; any submitted contact is
discarded for this method. Email and Telegram still require a valid contact.

Email validation supports common unquoted ASCII addresses, including plus
addressing and subdomains. International domains can use their ASCII punycode
form. Telegram accepts a standard username of 5–32 ASCII letters, digits or
underscores, starting with a letter, optionally prefixed by `@`. Usernames are
stored with one `@`; email domains are lowercased. Links, phone numbers, spaces,
and malformed contacts are rejected. Validation checks format, not ownership,
existence, or reachability; no verification email or Telegram request is sent.

The form works without JavaScript and uses multipart encoding to accommodate a
full cart of Unicode requests. Checkout bodies are limited to 384 KiB; account
fulfillment forms use 64 KiB for a maximum-size Unicode delivery and internal
note. Other non-upload forms retain their 16 KiB limit. Existing cart size and
quantity limits are unchanged.

On submit, CSRF, nonce, current cart revision, catalog fingerprint, and the
SQLite checkout claim are checked. Requests and contact fields are validated
before quote or wallet calls. Validation errors keep the entered values in the
response, mark the affected fields, release the claim, and issue a fresh nonce.
Only a valid completed review creates the invoice and opens the payment page.

## Storage and privacy

Schema 9 stores checkout details and account delivery without changing existing
payment or catalog rows:

- `order_checkout_details`: one delivery method per owned invoice, with an
  empty contact for My account and a required contact for email/Telegram;
- `order_item_requests`: one possibly blank request per saved invoice/service
  pair, linked to the corresponding immutable invoice item with a foreign key;
- `account_deliveries`: one customer-facing delivery message and publication
  time per completed account-delivery order.

Invoice, customer ownership, line snapshots, contact, requests, and cart clearing
are committed in one transaction. A failed save rolls everything back and leaves
the cart available for retry. The existing claim and revision checks continue to
prevent duplicate checkout or stale-cart commits.

Only the owning customer's `/account/orders/<invoice_id>` view and the
authenticated admin purchase-detail view render these fields. The shared
payment/status/QR routes do not expose them. Text is escaped and is never turned
into clickable external contact links. Inputs are not placed in session cookies,
URLs, wallet labels, or logs. Object representations omit contact, requests and
delivery bodies.
Existing orders without checkout details continue to render normally.

Contacts, requests and deliveries are stored in the restricted application
SQLite database and will be included in its backups. The application does not
encrypt them at rest or provide automatic end-to-end encryption. A customer or
administrator may instead paste a message they encrypted externally.

## Publishing an account delivery

After payment is settled and the service is complete, open the admin purchase
detail, enter the delivery for all items (1–12,000 characters), and select
Publish delivery and mark fulfilled. A PGP-encrypted message can be pasted here.
The internal fulfillment note is a separate field and is never shown to the
customer. Fulfillment still requires separate scope/authorization review.

Delivery creation and the fulfilled state commit together or neither is saved.
Validation/storage errors keep the submitted delivery and note in the response
for correction or retry. Repeating a completed submission does not overwrite
the existing delivery. CSRF, administrator authentication and settled-payment
checks are enforced server-side. Non-account orders cannot receive this message.

Customers open Account → Your orders → View order and delivery to read it.
Before publication their order displays No delivery yet. Only the owner and
administrator may read it; payment/status/QR bearer links never contain it.
This release supports a text delivery per order, not file uploads, editing
already-published deliveries, or automatic email/Telegram notifications.

## Required deployment migration: schema 8 → 9

There are no dependency or environment-variable changes. Pulling or restarting
alone does not migrate the database. Follow the verified online-backup procedure
in `deploy-xmr.md`, then stop the web service, pull the merged revision and run
the initializer with the protected environment loaded. Older recognized schemas
(including 7) can upgrade directly to 9:

```bash
systemctl stop servicesite-web.service
runuser -u servicesite -- git -C /opt/servicesite/app pull --ff-only origin main
cd /opt/servicesite/app
runuser -u servicesite -- /opt/servicesite/.venv/bin/python -c 'from pathlib import Path; from dotenv import load_dotenv; load_dotenv("/etc/servicesite/servicesite.env"); from app.config import Settings; from app.persistence import SQLiteDatabase, SCHEMA_VERSION; p=Settings.from_env().database_path; assert Path(p).is_file(), "Existing database not found; migration stopped"; db=SQLiteDatabase(p); db.initialize(); c=db.connect(); assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"; assert not c.execute("PRAGMA foreign_key_check").fetchall(); assert c.execute("SELECT value FROM schema_meta WHERE key=?", ("schema_version",)).fetchone()[0] == str(SCHEMA_VERSION); c.close(); print("Migration complete: schema", SCHEMA_VERSION)'
```

Stop if any command fails. After successful initialization:

```bash
systemctl start servicesite-web.service
curl -fsS http://127.0.0.1:5100/health
```

The initializer copies both existing checkout tables into their new constraints,
replaces them, verifies foreign keys and updates the version in one transaction.
Failure rolls back that replacement and version change; the additive delivery
table may already exist and can be reused on retry. Existing checkout rows,
invoices, accounts and carts are preserved. The initializer is safe to rerun.
Do not delete/recreate the database or restore an old backup just to change
schemas. Old code cannot handle the account delivery method or publish its
messages, and its initializer rejects schema 9. Do not roll back to old code
against this database; plan a compatible forward fix or an operator-reviewed
recovery preserving any newer order data.

## Verification boundary

Tests use temporary databases and fake rates/wallets. Coverage includes buy-now
with empty and existing carts, optional per-item requests, email/Telegram format
checks, error retention, maximum-size Unicode forms, CSRF, replay/stale review,
atomic storage rollback, ownership and admin visibility, escaped text, account
delivery publication gates and idempotency, and repeatable migration of existing
orders with rollback on interruption. No production database, real payment,
real delivery, or browser end-to-end verification is performed.
