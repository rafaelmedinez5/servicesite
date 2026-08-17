# XMR wallet-RPC transport and configuration

Status: Task 2 transport and Task 5 reconciliation-facing methods complete; no
live wallet connection or production payment behavior is enabled.

## Boundary

`app/payments/xmr_wallet_rpc.py` contains only amount conversion and wallet-RPC
transport behavior. It has no Flask, route, persistence, invoice, polling, or
fulfillment imports.

The client receives an immutable `WalletRpcConfig`, an optional HTTP session, and
an optional sleep function. Tests inject both dependencies and never connect to a
wallet. Every request uses HTTP Digest authentication.

## Amount rules

- XMR input is accepted only as a plain decimal string or `Decimal`.
- Binary floats and integers are rejected at the money boundary.
- Input must be positive, finite, and have no more than 12 decimal places.
- Conversion is exact; it never rounds or truncates.
- Internal values are integer atomic units bounded to SQLite's signed 64-bit
  integer range.
- Formatting returns a numeric decimal string without an `XMR` suffix so later
  Monero URI construction cannot accidentally include a currency label.

## Transport behavior

- The configured timeout is applied to every HTTP request.
- `XMR_RPC_RETRIES` is the total maximum attempt count, including the initial
  request. Attempts are limited to 1 through 10.
- Connection errors, timeouts, HTTP 429, and HTTP 5xx responses are retryable.
- Other HTTP failures, JSON-RPC errors, invalid JSON, missing result objects, and
  malformed method results fail immediately with typed, sanitized exceptions.
- Remote error messages and HTTP bodies are not included in exceptions.
- Retry delay is linear: configured backoff multiplied by the failed attempt
  number.
- `sweep_all` is deliberately non-retrying because a lost response could follow
  a successful broadcast. The orchestrator reconciles outgoing history before a
  later operator-visible retry.

The transport exposes `create_address`/`create_subaddress`, `get_height`, incoming
and outgoing transfers, transfer lookup by transaction ID, and `sweep_all`.
Invoice matching and state transitions remain outside this transport module.

## Environment contract

| Name | Purpose | Validation |
| --- | --- | --- |
| `ENVIRONMENT` | `development`, `test`, or `production` | Required enum |
| `SECRET_KEY` | Flask signing secret | Production: non-placeholder, at least 32 characters |
| `APP_HOST` | Web bind | Loopback only |
| `APP_PORT` | Web port | 1 through 65535; deployment value 5100 |
| `DB_PATH` | Fresh application database | Non-empty |
| `XMR_WALLET_RPC_URL` | wallet-RPC JSON-RPC endpoint | HTTP(S), no embedded credentials/query/fragment |
| `XMR_WALLET_RPC_USER` | Digest username | Production: explicit and non-placeholder |
| `XMR_WALLET_RPC_PASS` | Digest password | Production: non-placeholder, at least 16 characters |
| `XMR_ACCOUNT_INDEX` | Deposit account | Non-negative integer |
| `XMR_RPC_TIMEOUT` | Per-request seconds | 0.1 through 300 |
| `XMR_RPC_RETRIES` | Total attempts | 1 through 10 |
| `XMR_RPC_RETRY_BACKOFF` | Base delay seconds | 0 through 60 |
| `XMR_MIN_CONFIRMATIONS` | Canonical confirmation default | 1 through 1000; default 10 |
| `XMR_INVOICE_TTL_SECONDS` | Locked invoice payment window | 60 through 604800; default 7200 |
| `XMR_RATE_SOURCE` | Approved XMR/USD provider | Must be `coingecko` |
| `COINGECKO_API_KEY` | CoinGecko Demo credential | Production: explicit, secret, at least 16 characters |
| `XMR_RATE_TIMEOUT` | CoinGecko request seconds | 0.1 through 30; default 10 |
| `XMR_QUOTE_MAX_AGE_SECONDS` | Maximum provider quote age | 30 through 300; chosen policy 300 |
| `XMR_SWEEP_ENABLED` | Later sweep policy gate | Strict boolean; default false |
| `XMR_COLD_ADDRESS` | Later sweep destination | Production: required and non-placeholder only when sweeping is enabled |
| `XMR_SWEEP_ACCOUNT_INDEX` | Later sweep account | Non-negative integer |
| `XMR_SWEEP_PRIORITY` | wallet-RPC priority | Integer 0 through 4 |
| `XMR_SWEEP_RELAY` | Relay sweep transaction | Strict boolean |
| `XMR_SWEEP_RECONCILE_SECONDS` | Minimum outgoing-history observation window before retry | 30 through 3600; default 300 |
| `X_INTERNAL_TOKEN` | Internal poll authorization | Production: non-placeholder, at least 32 characters |
| `ALLOW_PUBLIC_XMR_WALLET_RPC` | Dangerous network override | Strict boolean; default false |

In production, wallet-RPC must resolve from a literal loopback/private IP or the
literal hostname `localhost`. Other hostnames and public IPs are rejected unless
`ALLOW_PUBLIC_XMR_WALLET_RPC=true` is deliberately set. That override does not
relax Digest authentication requirements.

## Differences from the private source client

- Configuration is injected rather than read from the environment by the client.
- Digest credentials are mandatory when constructing a client.
- Amount conversion rejects floats and excess precision instead of rounding down.
- HTTP, transport, JSON-RPC, and schema failures have distinct exception types.
- Retryable failures are explicit and bounded; malformed/remote errors are not
  retried blindly.
- Method results are schema-checked before returning typed values.
- Numeric XMR formatting omits the currency suffix.

Production wallet connectivity, wallet version compatibility, and real transfer
behavior remain unverified until a new wallet is created and tested on an
approved staged environment.
