# Sektor-7 Academy enrollment

## Release scope

The first Academy release provides a public three-month program page, customer
tier selection, a one-time USD 100 enrollment-fee invoice paid in XMR, and an
administrator enrollment list. It does not generate the later tuition invoices,
track lesson progress, host course media, or implement automatic recurring
payments.

Cryptocurrency payments are not automatically recurring. A later release must
create each monthly invoice explicitly and keep its state separate from the
initial enrollment fee.

## Pricing

The enrollment fee is additional to tuition. The selected tuition total is
stored with the enrollment so later price changes cannot rewrite an existing
student's agreement.

| Tier | Three-month tuition | Monthly schedule |
| --- | ---: | --- |
| Foundation | USD 500.00 | USD 166.67, USD 166.67, USD 166.66 |
| Operator | USD 1,500.00 | USD 500.00 each month |
| Black Tier | USD 5,000.00 | USD 1,666.67, USD 1,666.67, USD 1,666.66 |

## Payment and account behavior

- Enrollment requires a signed-in customer account, CSRF validation, and a
  single-use checkout nonce.
- A customer can hold only one Academy enrollment.
- The existing unfinished-order guard applies before enrollment begins.
- The enrollment fee uses the canonical XMR quote, unique subaddress, invoice,
  confirmation, sweep, and status flow.
- Cancelling an unpaid enrollment-fee order releases the Academy selection so
  the customer can choose again. Cancellation remains blocked after any payment
  is detected.
- The internal Academy catalog records are not published and cannot be edited
  through normal catalog administration.

## Administration

The protected Academy section lists the username, selected tier, locked tuition
total, initial payment state, and enrollment time. Its payment-state link opens
the existing redacted purchase-detail view. Password hashes, deposit addresses,
status tokens, and transaction identifiers are not displayed.

## Database upgrade

Schema 11 adds `academy_enrollments` and two unpublished internal catalog
records used by the existing invoice foreign-key boundary. The initializer
upgrades schema 10 in place and preserves accounts, catalog records, orders,
invoices, inquiries, and payment state. Back up the SQLite database, stop the
web service, pull the reviewed revision, run `SQLiteDatabase.initialize()`, and
restart only after the initializer succeeds.
