# Saved carts and customer orders

## Database migration required

This release requires **schema 5 → 6**. Pulling source or restarting the web
service does not run the migration automatically.

Use the existing operator procedure in `deploy-xmr.md`: take and verify a SQLite
online backup, stop the web service, load the protected production environment,
run `SQLiteDatabase.initialize()`, and restart only after it succeeds. Do not
delete or recreate the production database. The initializer also supports
recognized schema versions 1 through 4 and is safe to rerun.

The additive migration creates:

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
The cart clears only when invoice, ownership, and all item snapshots have been
committed together. Direct single-service checkout remains available and also
creates a customer-owned order.

`/account` lists the 100 most recent owned orders. Each order detail checks the
signed-in customer ID and returns the same 404 for an unknown or other
customer's order. Private bearer payment/status links continue to work without
login for compatibility. Customers should keep those links private.

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
