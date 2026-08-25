# Service checkout and private payment status

Status: Task 4 complete in code and automated tests. No production wallet,
database, CoinGecko key, payment, or polling job was accessed.

## Route map

| Method | Route | Purpose | Access |
| --- | --- | --- | --- |
| `GET` | `/` | Published service catalog and order forms | Public |
| `POST` | `/checkout` | Validate form, obtain quote, create one invoice | CSRF plus single-use form nonce |
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
- Each catalog response issues a separate, single-use checkout nonce. Replaying
  a successful form cannot create a second invoice.
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
