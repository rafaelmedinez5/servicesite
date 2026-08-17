# Application decisions

Status: confirmed for the Task 0 scaffold on 2026-08-17.

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
- Checkout converts USD into XMR using an approved rate source.
- Invoice creation locks the service/category description, USD price, exchange
  rate, expected XMR atomic amount, and quote timestamp.
- Later catalog edits never alter historical purchase or invoice snapshots.
- The production rate source and maximum quote age remain blocked until the XMR
  configuration task.

## Administration

- The first release has one administrator account.
- Only a generated password hash may be stored in external configuration.
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

## Deferred decisions

- initial categories and service packages;
- production XMR/USD rate source and quote-age limit;
- record-retention periods;
- customer report and delivery workflow;
- production sweep policy.

Initial catalog contents and prices are not build blockers because the admin
workflow will create them after deployment.
