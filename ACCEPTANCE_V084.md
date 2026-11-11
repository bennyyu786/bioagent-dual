# v0.8.4 acceptance report

## Tested locally

- All historical v0.8.3 regression tests
- Atomic concurrent dispatch, priority and deterministic resource-aware selection
- Administrator RBAC, Viewer denial, tenant-scoped jobs and trusted Worker token
- Only approved capability + declared Worker adapter can be scheduled
- Explicit Worker claim updates TaskCenter; duplicate claim refused
- Results are reviewed independently of Worker self-report; result hash enforcement and admin-only evidence retrieval
- Rejected results terminate task; successful verification completes task and frees resources
- `reported` review state does not expire and retains resources even after original execution TTL
- Explicit unclaimed-offer cancellation and expired-lease quarantine
- Trusted local subprocess adapter executes fixed argv with JSON stdin and no inherited API credential
- Local ASGI TestClient synthetic end-to-end demonstration
- Python compile, JavaScript syntax and ZIP CRC validation

## Not tested / not production certified

- Real networked Claude Code, Codex, Biomni A1 adapters; production model/provider credentials
- Physical GPU runtime telemetry or resource enforcement (only ledger accounting)
- Multi-host coordinator HA, Worker fencing, hard job cancellation or long-duration lease renewal
- External TLS/Caddy deployment, 7×24 soak, failover and load tests
- Human-reviewed scientific evidence quality or independent external validator correctness
- Visual browser automation; local runtime browser restrictions

**Correct label:** v0.8.4 integrated reference control-plane and local Worker execution bridge, not fully production-certified distributed orchestration.
