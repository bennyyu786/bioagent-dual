# v0.7.1 changes (from v0.7.0)

- Added `biomni_dual/observability.py`, `operations_runtime.py`.
- Added optional observed daemon mode, aggregate metrics, explicit container bind opt-in, service and remote worker heartbeats, persistent cooldown alerts.
- Added Docker Compose/systemd deployment templates and operator CLI.
- Added live read-only acceptance script.
- Fixed v0.7.0 initial idle tick incorrectly skipping execution when monotonic clock uptime was lower than configured idle interval.
- Kept a single `biomni_dual/` package. No new v071 source subpackage.

See DEPLOYMENT_V071.md and ACCEPTANCE_V071.md. Real Docker/SDK/MCP integration is unverified here.
