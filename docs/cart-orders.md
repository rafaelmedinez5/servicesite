# Saved carts and customer orders

## Database migration required

The current checkout release requires **schema 9 → 10**. Schema 10 adds a
nullable cancellation timestamp to each customer-owned order. The original
cart release introduced schema 5 → 6 and checkout review introduced later
schema updates.
Pulling source or restarting the web
service does not run the migration automatically.

Use the existing operator procedure in `deploy-xmr.md`: take and verify a SQLite
online backup, stop the web service, load the protected production environment,
run `SQLiteDatabase.initialize()`, and restart only after it succeeds. Do not
delete or recreate the production database. The initializer also supports
recognized schema versions 1 through 7 and is safe to rerun.

The schema 10 migration preserves all existing data and adds:

- `customer_orders.cancelled_at`: records a customer cancellation without
  deleting the invoice or changing payment reconciliation.

The original cart migration created:

- `customer_carts`: one revision counter per customer;
- `cart_items`: service IDs and quantities owned by that customer;
- `cart_checkout_claims`: short-lived concurrency leases;
- `customer_orders`: the invoice-to-customer ownership relation;
- `invoice_items`: immutable service/category/duration/price/quantity snapshots.

Existing accounts, password hashes, catalog rows, invoices, payment amounts,
addresses, and bearer links are preserved. Earlier invoices have no recorded
customer owner, so they are not guessed into an account's history.

## Customer flow

Customers sign in, add services from their detail pages, and review `/cart`.
The cart survives logout and is visible from another signed-in browser. Each
cart supports up to 20 different services and 1–10 units of each. A quantity of
zero removes an item. Archived or unpublished services are shown as unavailable
and must be removed before checkout.

Checkout sums current integer USD cents, then converts that total once into
the exact XMR atomic amount. It creates one invoice with one unique address.
The cart clears only when invoice, ownership, item snapshots, delivery contact,
and requests have been committed together. `/cart/checkout` first collects a
request for each service line and a validly formatted email or Telegram contact.
Buy-now adds the selected service if missing, preserves existing quantities,
and opens this review page without creating an invoice.

Only one non-cancelled order in an unfinished payment state is allowed per
customer. The guard runs before a rate quote or wallet subaddress request and
is rechecked inside the invoice persistence transaction to prevent concurrent
workers from creating multiple orders. Settled and expired orders do not block
checkout. A customer can cancel only an `awaiting_payment` order for which no
funds have been observed. Cancellation hides payment actions and releases the
checkout guard. The invoice remains in the payment poller's normal open set
until its original expiry so an accidental late transfer is not silently
ignored.

`/account` lists the 100 most recent owned orders. Each order detail checks the
signed-in customer ID and returns the same 404 for an unknown or other
customer's order. Private bearer payment/status links require a signed-in site
session and never show requests or delivery contacts. Customers should keep
those links private.

## Price and concurrency guarantees

- Cart edits increment a durable revision counter.
- Checkout compares that revision and a fingerprint of the reviewed service
  snapshots before calling the quote provider or wallet. A changed price,
  description, publication state, or quantity requires another review.
- A SQLite `BEGIN IMMEDIATE` transaction claims the cart revision for five
  minutes. Concurrent requests cannot create an invoice for the same revision.
- After a crashed worker, a new request can take over an expired lease. The old
  worker's claim token then fails the final transaction, so it cannot persist
  a second invoice.
- The final transaction rechecks every service, the cart revision, and the
  claim token; saves invoice, ownership, and items; then clears the cart and
  increments its revision atomically.
- Known failures release the claim and preserve the cart. A failed commit after
  wallet allocation may leave an unused wallet subaddress, never a partial
  order. That address is not reused.

All writes require the customer session and CSRF. Cart checkout also requires
the existing single-use form nonce. Client totals and owner fields are ignored.
Payment confirmation, expiry, sweep, and fulfillment rules are unchanged.
Fulfillment currently applies to the entire order, not individual lines.

## Verification boundary

Automated tests use fake rates/wallets and temporary SQLite databases. They cover
ownership, quantities, snapshot totals, stale reviews, concurrent claims,
lease takeover, failure rollback, old bearer links, and migration preservation.
No live wallet payment, production migration, or browser end-to-end test is
performed by this change.

## Schema 9 to 10 operator procedure

Use the normal verified SQLite backup procedure, stop the web service, pull the
reviewed revision, then run the existing initializer with the production
environment loaded:

```bash
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && exec /opt/servicesite/.venv/bin/python -c "from pathlib import Path; from dotenv import load_dotenv; load_dotenv(\"/etc/servicesite/servicesite.env\"); from app.config import Settings; from app.persistence import SQLiteDatabase, SCHEMA_VERSION; p=Settings.from_env().database_path; assert Path(p).is_file(), \"Existing database not found; migration stopped\"; db=SQLiteDatabase(p); db.initialize(); c=db.connect(); assert c.execute(\"PRAGMA integrity_check\").fetchone()[0] == \"ok\"; assert not c.execute(\"PRAGMA foreign_key_check\").fetchall(); assert c.execute(\"SELECT value FROM schema_meta WHERE key=?\", (\"schema_version\",)).fetchone()[0] == str(SCHEMA_VERSION); assert \"cancelled_at\" in {row[1] for row in c.execute(\"PRAGMA table_info(customer_orders)\")}; c.close(); print(\"Migration complete: schema\", SCHEMA_VERSION)"'
```

If the inquiry tables from `docs/contact-join-inquiries.md` have not yet been
initialized on this host, run that documented initializer separately before
starting the web service.
