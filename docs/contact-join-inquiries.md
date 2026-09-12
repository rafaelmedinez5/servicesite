# Contact and Join inquiry forms

This feature adds two separate account-linked forms while keeping `/contact` and `/join` readable without an account.

## Behavior

- Contact requests may be submitted repeatedly, with a limit of five requests per account per rolling hour.
- Join applications may be submitted only once per customer account. The application form disappears after submission and the database also enforces `UNIQUE(customer_id)` so concurrent requests cannot create a duplicate.
- Both submission endpoints require an authenticated customer session and CSRF token.
- Administrators review both queues at `/admin/inquiries` behind the existing administrator session protection.
- No file uploads are accepted. Initial messages must not contain credentials, private keys, wallet seeds, live targets, or confidential client material.

## Security boundary

All submitted values are validated server-side. Topic and specialty fields use fixed allow-lists; reply addresses reuse the existing email/Telegram validator; text fields have explicit minimum/maximum lengths and reject unsupported control characters. SQLite statements use bound `?` parameters for every user-controlled value. Jinja's default autoescaping remains enabled in the admin view and no submitted field is rendered with `safe`. Existing CSP, `form-action 'self'`, no-JavaScript policy, secure session settings, and admin/customer authentication continue to apply.

The form does not attempt to make stored messages end-to-end encrypted. Use the published PGP key for sensitive follow-up.

## Database initialization

The inquiry tables are additive to the existing servicesite database and carry their own `schema_meta` key, `inquiries_schema_version=1`. They do not change invoice, payment, wallet, order, or catalog tables.

Before deployment, stop the web service and create/verify the normal SQLite online backup. Pull the reviewed revision and initialize the main schema first, then the inquiry tables:

```bash
systemctl stop servicesite-web.service
runuser -u servicesite -- git -C /opt/servicesite/app pull --ff-only origin main
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && exec /opt/servicesite/.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(\"/etc/servicesite/servicesite.env\"); from app.config import Settings; from app.persistence import SQLiteDatabase; from app.inquiries import InquiryStore; db=SQLiteDatabase(Settings.from_env().database_path); db.initialize(); InquiryStore(db).initialize()"'
```

Stop if initialization raises an exception. After it succeeds, run the test suite, then start and health-check the service:

```bash
runuser -u servicesite -- sh -c 'cd /opt/servicesite/app && /opt/servicesite/.venv/bin/python -m pytest'
systemctl start servicesite-web.service
curl -fsS http://127.0.0.1:5100/health
```

The inquiry initializer is idempotent for version 1. Do not manually create the tables or edit `schema_meta`.
