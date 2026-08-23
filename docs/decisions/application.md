# Application decisions

Status: application and Task 4 pricing decisions confirmed on 2026-08-17.

## Catalog and public pages

- Categories and services are created and maintained through an admin panel.
- Only published categories and services are displayed publicly.
- Services have a name, slug, description, USD price, optional access duration,
  publication state, archive state, and sort order.
- Categories have a name, slug, description, publication state, archive state,
  and sort order.
- The application is server-rendered with Flask/Jinja and CSS; no JavaScript.

## Pricing and purchase integrity

- The administrator enters service prices in USD.
- Checkout converts USD into XMR using the CoinGecko Demo price endpoint.
- The provider timestamp may be no more than 300 seconds old. Missing, stale,
  malformed, rate-limited, or unavailable quotes fail checkout without creating
  an invoice or using a fallback rate.
- The CoinGecko API key is stored only in external production configuration and
  is never rendered, logged, or committed.
- Invoice creation locks the service/category description, USD price, exchange
  rate, expected XMR atomic amount, and quote timestamp.
- Later catalog edits never alter historical purchase or invoice snapshots.

## Administration

- The first release has one administrator account.
- First use creates the password through a one-time server-rendered setup form;
  only its generated Werkzeug hash is stored in the protected SQLite database.
- Changing the password requires the current password and invalidates every
  existing administrator session.
- An optional six-digit recovery PIN may provide an alternative login after
  password setup; it requires the configured username and shares the password
  login rate limit.
- Admin routes require authenticated sessions, CSRF protection, login rate
  limiting, and session expiry.
- Purchases can be filtered by category, service, date, payment status, and
  fulfillment status.
- Sensitive bearer tokens, credentials, and complete payment identifiers are
  not displayed in routine list views.
- Categories/services referenced by purchases are archived instead of deleted.

## Fulfillment

- Payment state and fulfillment state are separate.
- Fulfillment is manual for the initial release.
- An administrator may fulfill only a settled purchase.
- A settled payment does not by itself authorize a cybersecurity engagement;
  authorization and scope remain separate operator checks.

## Runtime

- The application listener is loopback-only at `127.0.0.1:5100`.
- External access is through the separately configured Tor onion service.
- Production configuration and secrets remain outside Git.

## Still blocked

- initial categories and service packages;
- record-retention periods;
- customer report and delivery workflow;
- production sweep policy.

Initial catalog contents and prices are not build blockers because the admin
workflow will create them after deployment.
