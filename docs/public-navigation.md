# Public pages and navigation

The public homepage shows every published, non-archived category and its
published services. Each category also has a dedicated `/categories/<slug>`
page for focused browsing and direct links. Service detail pages recommend up
to three other services, prioritizing the current category before the rest of
the catalog.

The catalog is session-gated. Anonymous visitors are redirected to customer
login and may access only Login, Register, About, Join, Contact, the PGP-key
page, and Admin Login. Health checks and the independently authenticated
internal polling routes remain available to their existing callers. A valid
customer or administrator session unlocks the catalog; customer-only shopping
routes continue to require a customer session.

The desktop header uses three balanced zones: the brand on the left,
About/Join/Contact in the center, and Categories/Cart/account actions on the
right. The brand links directly to the homepage, so the header does not
duplicate it with a Services link. Signed-in customers receive a compact
account dropdown with overview, transaction-history, and logout actions. At
tablet and mobile widths, the primary links, category links, cart, and account
actions move into a `details`-based menu. All dropdowns work without JavaScript
and preserve the application's no-script content security policy.

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

The centered footer contains only About, Join, Contact, a link to `/pgp-key`,
and the configured onion hostname. Set the optional Tor v3 hostname in the
protected application environment:

```text
PUBLIC_ONION_ADDRESS=<56-character-v3-hostname>.onion
```

The value is normalized to a hostname and rendered as plain text. The PGP page
does not publish a placeholder key; it clearly reports that the operator's
armored public key is pending until the real key is supplied.
