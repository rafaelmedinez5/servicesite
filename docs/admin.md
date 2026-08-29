# Administrator workflow

Status: implemented and covered by local regression tests; target-host
configuration and Tor-browser verification remain operator actions.

## Boundary

The first release has one administrator account at `/admin/login`. The panel is
fully server-rendered and contains no JavaScript. It supports:

- creating, editing, publishing, and archiving categories;
- creating, editing, publishing, and archiving USD-priced services;
- uploading, replacing, and removing privacy-sanitized service images;
- purchase filtering by category, service, UTC date, payment state, and
  fulfillment state;
- purchase detail without bearer status tokens, deposit addresses, or deposit/
  sweep transaction identifiers; and
- marking a purchase fulfilled only after its payment reaches `settled`.

Cart orders appear as one purchase with all saved service lines and the customer
username on the detail page. Category and service filters match any saved line.
Fulfillment applies to the whole order and remains blocked until its single
invoice is settled. Admin views still omit payment bearer tokens, deposit
addresses, and transaction identifiers.

Catalog edits never change an existing invoice snapshot. Archival replaces
deletion. Fulfillment is a separate state and cannot settle an invoice or begin
an authorized security engagement by itself.

## Service-image privacy boundary

An authenticated service edit page accepts one JPEG, PNG, or WebP file up to
5 MB and 24 million source pixels. Animated images and every other format are
rejected. The application applies EXIF orientation, center-crops every upload to
the same 8:5 frame without upscaling it, bounds the output to 1600 by 1000
pixels, copies only decoded pixels into a new RGB image, and writes a fresh WebP
without EXIF, XMP, comments, color profiles, the source filename, or the source
bytes. The homepage, service detail, and administrator preview all reserve that
same frame so existing and newly uploaded images cannot stretch the layout.

Derivatives are stored below `/opt/servicesite/instance/service-images`, not in
Git or SQLite. The directory is mode `0700`, each file is mode `0600`, and every
stored name is a random 256-bit hexadecimal key unrelated to the uploaded
filename. A public image request must match both the current key and a currently
published service. Replacement, removal, and service archival immediately revoke
the previous URL; the old derivative is then deleted on a best-effort basis.

These controls prevent ordinary clients from extracting camera metadata or the
administrator's local filename. They are not a complete anonymity guarantee.
The administrator must inspect the visible pixels for faces, names, addresses,
documents, reflections, recognizable locations, watermarks, or other identifying
content. Do not assume re-encoding removes every deliberate pixel-level or
steganographic fingerprint. Publication timing and a compromised host can also
reveal information that image re-encoding cannot remove.

## Authentication controls

- an empty credential table exposes the one-time setup form at `/admin/login`;
- the setup form stores only a generated Werkzeug hash in the protected SQLite
  database;
- password changes require the current password and invalidate all admin
  sessions;
- an optional six-digit `ADMIN_RECOVERY_PIN` provides an alternative login only
  after password setup and requires the configured username;
- the plaintext password is never stored in Git or shell history;
- sessions expire after `ADMIN_SESSION_HOURS` and are not extended on requests;
- production cookies are Secure, HttpOnly, and SameSite Strict;
- every state-changing form requires the session CSRF token;
- five failed logins within fifteen minutes create a fifteen-minute global
  block recorded in SQLite; and
- every admin response is `no-store`, `noindex`, no-referrer, frame-denied, and
  governed by the no-script content security policy.

The global limit is intentional because Tor forwards onion requests from a
loopback source. It can cause a temporary administrator lockout during a focused
attack; do not weaken it without replacing it with a stronger boundary.

## Optional recovery PIN

Set `ADMIN_RECOVERY_PIN` to exactly six ASCII digits in the protected external
environment file and restart the web service. Leave it absent or empty to
disable PIN login. The password login page shows the recovery link only when the
PIN is enabled and the normal password has already been initialized.

A six-digit PIN is much weaker than the administrator password. The recovery
form therefore requires the configured username and shares the same global
five-attempt, fifteen-minute block with password login. The PIN is never
rendered or logged, but it is stored as a secret in the mode-`0600` environment
file. A PIN-authenticated session still needs the current password to change the
password.

## First-time password setup

After the schema migration, visit `/admin/login` through Tor while the onion
hostname is still private. When no administrator credential exists, the page
asks for a new password and confirmation. A successful submission hashes the
password with Werkzeug, stores only the hash in SQLite, and signs in the
administrator. The setup form disappears immediately and cannot be reused.

Anyone who can reach an uninitialized setup page can claim the administrator
account. Complete setup before sharing the onion hostname. Keep
`ADMIN_USERNAME` at the locally selected value and set `ADMIN_SESSION_HOURS`
between 1 and 24 in the external environment.

The Security link in the administrator navigation changes the password. It
requires the current password and signs out every active administrator session,
including the session that submitted the change.

## Upgrade an existing schema-version-2 through schema-version-6 database

1. Keep the payment timer disabled.
2. Take an online SQLite backup and verify `PRAGMA integrity_check` returns
   exactly `ok`.
3. Stop `servicesite-web.service`; wallet-RPC may remain active.
4. Pull the reviewed application revision and install locked dependencies,
   including Pillow for the decode-and-re-encode boundary.
5. Configure `ADMIN_USERNAME` and `ADMIN_SESSION_HOURS`. Optionally configure
   `ADMIN_RECOVERY_PIN`; no password or password hash belongs in the environment
   file.
6. Run `SQLiteDatabase.initialize()` using the command in `deploy-xmr.md`.
7. Start the web service, run runtime preflight, and complete the one-time setup
   through Tor before sharing the onion hostname.

Schema version 7 adds a nullable random image key to each service. Existing
catalog, customer, order, invoice, and credential rows are preserved. Image
bytes are never placed in the database. Follow the backup, stop, initialize,
and restart sequence in `deploy-xmr.md`; do not add the column manually.

Do not create or fulfill a real purchase until stagenet payment verification is
complete. Keep the polling timer disabled until the Task 8 acceptance matrix is
approved.
