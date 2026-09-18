# Sektor-7 Academy enrollment

## Release scope

The Academy release provides a public three-month program page, one USD 300
program plan, a USD 100 month-one invoice paid in XMR, and an administrator
enrollment list. It does not generate the later monthly invoices,
track lesson progress, host course media, or implement automatic recurring
payments.

Cryptocurrency payments are not automatically recurring. A later release must
create each later monthly invoice explicitly and keep its state separate from
the month-one payment.

## Pricing

The full three-month program costs USD 300. There is no enrollment fee. The
student pays USD 100 for month one when enrolling, then USD 100 for each of
months two and three. The course total is stored with the enrollment so later
price changes cannot rewrite an existing student's agreement.

| Plan | Three-month total | Monthly schedule |
| --- | ---: | --- |
| Academy Program | USD 300.00 | USD 100.00 each month |

## Payment and account behavior

- Enrollment requires a signed-in customer account, CSRF validation, and a
  single-use checkout nonce.
- A customer can hold only one Academy enrollment.
- The existing unfinished-order guard applies before enrollment begins.
- The month-one payment uses the canonical XMR quote, unique subaddress, invoice,
  confirmation, sweep, and status flow.
- Cancelling an unpaid month-one order releases the Academy enrollment so the
  customer can enroll again. Cancellation remains blocked after any payment
  is detected.
- The internal Academy catalog records are not published and cannot be edited
  through normal catalog administration.

## Administration

The protected Academy section lists the username, program, locked course total,
month-one payment state, and enrollment time. Its payment-state link opens
the existing redacted purchase-detail view. Password hashes, deposit addresses,
status tokens, and transaction identifiers are not displayed.

## Database upgrade

Schema 11 adds `academy_enrollments` and two unpublished internal catalog
records used by the existing invoice foreign-key boundary. The initializer
upgrades schema 10 in place and preserves accounts, catalog records, orders,
invoices, inquiries, and payment state. Back up the SQLite database, stop the
web service, pull the reviewed revision, run `SQLiteDatabase.initialize()`, and
restart only after the initializer succeeds.
