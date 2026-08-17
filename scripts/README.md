# Operational scripts

`poll-xmr` derives the installation root from its own location and executes the
tested Python polling client. The Python client obtains the loopback host, port,
and internal token from validated application settings. The token remains in the
process environment and HTTP header; it is never placed in command arguments or
printed.

The client disables proxy discovery for this loopback request, applies a bounded
HTTP timeout shorter than the systemd runtime limit, validates the sanitized JSON
summary, and emits counters only. It never reads a wallet, database, or secret
file directly.

`verify-systemd` creates temporary syntax-only copies of the committed units and
replaces their commands with `/bin/true` before calling `systemd-analyze verify`.
This separates parser/hardening errors from expected missing target-host binaries.
It does not install, reload, enable, or start a unit.
