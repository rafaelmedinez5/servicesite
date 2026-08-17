# Sanitized systemd templates

Task 6 provides four new, non-legacy units:

| Unit | Identity | Purpose |
| --- | --- | --- |
| `servicesite-wallet-rpc.service` | `xmrwallet:xmrwallet` | Dedicated wallet-RPC using one protected external config file |
| `servicesite-web.service` | `servicesite:servicesite` | Gunicorn on the application-configured loopback listener |
| `servicesite-poll-xmr.service` | `servicesite:servicesite` | One protected reconciliation request |
| `servicesite-poll-xmr.timer` | system timer | Coalesced one-minute polling schedule |

These files contain no credentials, wallet paths, daemon endpoint, cold address,
or internal token. The wallet service passes only the officially documented
`--config-file` option; Task 7 must install a selected Monero release and create
the external configuration after its supported options are verified locally.

The units have not been copied, enabled, or started. Follow
`docs/systemd-install.md`; every live-host mutation is an operator action.
