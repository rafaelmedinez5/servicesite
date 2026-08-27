# Service checkout and private payment status

Status: Task 4 complete in code and automated tests. No production wallet,
database, CoinGecko key, payment, or polling job was accessed.

## Route map

| Method | Route | Purpose | Access |
| --- | --- | --- | --- |
| `GET` | `/` | Published service catalog and service-detail links | Public |
| `GET` | `/services/<service_slug>` | Published service information and account handoff/order form | Public |
| `GET, POST` | `/register` | Create a customer username and password | Public form; CSRF on submit |
| `GET, POST` | `/login` | Start a customer session | Public form; CSRF and per-account rate limit |
| `GET` | `/account` | Show the signed-in customer account | Customer session |
| `POST` | `/logout` | End the customer session | Customer session plus CSRF |
| `POST` | `/checkout` | Validate form, obtain quote, create one invoice | Customer session, CSRF, and single-use form nonce |
| `GET` | `/checkout/<invoice_id>/<status_token>` | Locked amount, unique address, expiry, and links | Invoice bearer token |
| `GET` | `/checkout/<invoice_id>/<status_token>/qr.png` | Monero payment QR | Invoice bearer token |
| `GET` | `/status/<invoice_id>/<status_token>` | Customer-safe payment state | Invoice bearer token |
| `GET` | `/health` | Process health response | Public loopback health check |

There is no customer-triggered wallet-RPC refresh route. The status page reads
the last transactionally stored invoice state. Task 5 adds a separate protected
polling boundary; no timer is installed or enabled at this checkpoint.

## Pricing policy

The approved provider is the CoinGecko Demo simple-price endpoint for Monero in
USD. Requests ask for full precision and `last_updated_at`. JSON decimal values
are parsed directly into `Decimal`, never binary float. The provider timestamp
must be no more than 300 seconds old.

`COINGECKO_API_KEY` is required in production and omitted from representations.
Provider timeouts, non-200 responses, malformed values, missing timestamps,
future timestamps, and stale quotes return a generic checkout-unavailable page.
The route creates no invoice and never calls the wallet after quote failure.
There is no hard-coded or cached indefinite fallback.

## Form and route security

- The signed Flask session carries a strong CSRF token.
- Published service details remain public, but only a signed-in customer sees
  the purchase form. Anonymous requests receive login and registration links,
  and a direct anonymous checkout POST is rejected before rate or wallet access.
- Each signed-in service-detail response issues a separate, single-use checkout
  nonce. The catalog and anonymous detail pages do not issue checkout tokens or
  create invoices directly.
  Replaying a successful detail-page form cannot create a second invoice.
- Unknown, unpublished, archived, or category-hidden service slugs return the
  same generic 404 response and do not reveal catalog state.
- Service identity and current publication state are revalidated server-side,
  then rechecked transactionally by the canonical invoice creator.
- Checkout, QR, status, private errors, and redirects use `no-store` and
  `X-Robots-Tag: noindex, nofollow, noarchive`.
- All responses deny scripts, framing, cross-origin referrers, MIME sniffing,
  camera, microphone, geolocation, and browser payment APIs.
- Wrong bearer tokens return the same 404 as an unavailable private resource.

## Monero URI and customer state

The PNG QR encodes `monero:<unique-address>?tx_amount=<numeric-decimal>`. PNG is
used instead of SVG so the QR remains visible at Tor Browser's Safest security
level. The amount comes from integer atomic units and contains no `XMR` suffix.

Customer pages expose the service snapshot, locked amount, expiry, invoice ID,
unique payment address where required, confirmation progress, and simplified
payment state. They do not expose wallet account/subaddress indexes, internal
tokens, transaction IDs, RPC errors, raw internal status names, or sweep details.

Partial, pending-confirmation, finalizing, expired, and settled states have
separate customer messages. Every state before `settled` says fulfillment has
not started. A settled invoice is only eligible for manual fulfillment review;
payment does not authorize a cybersecurity engagement.

## Deferred and unverified

- live CoinGecko access through the production network;
- live wallet-RPC invoice creation and QR scanning with a Monero wallet;
- live transfer matching, confirmations, expiry, and sweep behavior;
- admin authentication, catalog CRUD, purchase visibility, and manual
  fulfillment actions;
- production database initialization and deployment.
