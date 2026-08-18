# Systemd template decisions

Status: Task 6 template decisions recorded on 2026-08-17 and Task 7 preflight
boundary recorded on 2026-08-17. Installation and runtime validation remain
separate operator gates.

- Components use separate web, wallet-RPC, XMR poll, and timer units.
- Gunicorn and the poll client run with the application identity; wallet-RPC
  runs with its separate wallet identity.
- The XMR poll uses a persistent calendar timer once per minute. Missed runs are
  coalesced and the oneshot service cannot overlap with itself.
- The web listener is derived from validated application settings and must stay
  loopback-only.
- The poll client calls the protected loopback endpoint without placing its
  internal token in process arguments.
- The dedicated wallet unit passes only a protected external configuration-file
  path. No wallet, daemon, authentication, or proxy options are embedded.
- Task 7 pins the official Linux x64 Monero `v0.18.5.1` archive. Signature,
  hash, version, required options, external configuration, and runtime remain
  target-host operator checks; the repository did not install a binary.
- Unit installation, daemon reload, enablement, and service starts are operator
  actions and were not performed in Task 6.
- The Task 7 preflight has separate install/runtime modes and performs only
  captured metadata, status, loopback connection, fixed web health, and wallet
  `get_height` checks. It never operates a service or payment state.
