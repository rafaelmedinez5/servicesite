# Public pages and navigation

The public homepage is a category directory. Each published, non-archived
category has a dedicated `/categories/<slug>` page containing its published,
non-archived services. Service detail pages recommend up to three other
services, prioritizing the current category before the rest of the catalog.

The desktop header keeps categories inside an accessible `details` dropdown.
At tablet and mobile widths, the primary links, category links, cart, and
account actions move into a `details`-based menu. Both controls work without
JavaScript and preserve the application's no-script content security policy.

The About and Join pages describe the authorization and practitioner boundaries.
The Contact page deliberately does not accept or store messages. An operator can
publish one external contact channel by setting both values below in the
protected application environment and restarting the web service:

```text
PUBLIC_CONTACT_METHOD=Session
PUBLIC_CONTACT_ADDRESS=<public address>
```

Both values are optional, but they must be configured together. The address is
rendered as plain text rather than an automatic external link. This release does
not change the SQLite schema and requires no database migration.
