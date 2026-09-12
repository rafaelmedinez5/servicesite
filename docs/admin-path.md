# Private administrator URL path

The administrator blueprint supports a deployment-specific URL prefix through `ADMIN_PATH`.

This is defense in depth only. A non-obvious path reduces routine scanning and noise, but administrator authentication, rate limiting, CSRF protection, secure sessions, and the existing response security headers remain the real security boundary.

## Production setup

Do not commit the live path to Git. Store it only in `/etc/servicesite/servicesite.env`.

Generate a random path segment on the server, for example:

```bash
ADMIN_SEGMENT="control-$(/opt/servicesite/.venv/bin/python -c 'import secrets; print(secrets.token_hex(16))')"
printf 'ADMIN_PATH=/%s\n' "$ADMIN_SEGMENT"
```

Add the printed `ADMIN_PATH=/...` line to `/etc/servicesite/servicesite.env` using the same restricted permissions as the rest of that file. Do not paste the production value into tickets, GitHub, screenshots, or normal logs.

The value must be one lowercase URL segment, 3–64 characters after the leading slash, using letters, numbers, and hyphens. It cannot collide with a public application route.

Production refuses to start when the effective path remains `/admin`. Development and tests retain `/admin` as the compatibility default.

After changing the external environment file, restart `servicesite-web.service`. The old `/admin` URL is not registered when a custom prefix is active and should return 404.

No SQLite migration is required for this change.
