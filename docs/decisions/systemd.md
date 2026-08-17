# Systemd template decisions

Status: Task 6 template decisions recorded on 2026-08-17. Installation and
runtime validation remain separate operator gates.

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
- The wallet-RPC binary is not installed. Its version, options, external
  configuration, and runtime remain blocked until Task 7 verification.
- Unit installation, daemon reload, enablement, and service starts are operator
  actions and were not performed in Task 6.
