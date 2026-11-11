# v0.7.0 acceptance matrix

| Area | Evidence | Status |
|---|---|---|
| Persistent events | SQLite queue, event ID, immutable deduplication key | Offline tests passed |
| Environment observation | Allowlisted probes; first baseline; transition detection and digests | Offline tests passed |
| Recurring checks | Durable schedule table; skips missed intervals after restart | Offline tests passed |
| Authenticated webhook | HMAC-SHA256 + nonce with durable replay protection | Offline tests passed (adapter only; no deployed HTTPS server) |
| Multi-timescale decisions | Fast/medium/slow cadence with persistent last-tick record | Offline tests passed |
| Cooperative coordination | Per-goal expiring lease + heartbeat + token-specific release | Offline tests passed |
| Crash-safe response | Interrupted RUNNING task quarantined, manually reconciled | Offline tests passed |
| Goal lifecycle | Active, paused, waiting, completed, exhausted, stopped | Offline tests passed |
| Controlled runtime | Serve loop and explicit trusted factory daemon | Offline tests passed |
| Audit | Hash-chained state transitions and decision entries | Offline tests passed |
| Earlier features | v0.6.7 functional regression tests | 251 passed |
| Newly added tests | Persistent environment/runtime tests | 23 passed |
| Overall | `pytest -q` | **274 passed** |
| Live systemd/Kubernetes | Daemon deployed, restarted, observed for long duration | **Not tested here** |
| Claude/Codex/Biomni/MCP/Docker | Real authenticated and isolated end-to-end integrations | **Not tested here** |
| Distributed worker fencing | Cross-host leases / transactional task hand-off | **Not implemented** |

## Safety and completion interpretation

This is a *persistent single-host runtime prototype*, not proof of 24/7 production
availability. Controller decisions can be retried after a crash (at-least-once),
therefore downstream actions must be idempotent or require human reconciliation.
Webhooks require an external HTTPS web server and rate-limit policy. The operator
must review external effects before releasing blocked tasks.
