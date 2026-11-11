# v0.7.2 Web dashboard (read-only)

The dashboard is served by the existing `ObservabilityServer` and uses its **real** SQLite-backed operations snapshot. No frontend dependencies or CDN are required. The dashboard intentionally never exposes task payloads, user research inputs, API keys, worker tokens or remote execution controls.

```bash
pip install -e '.[test]'
python scripts/dashboard_server.py --ops-db operations.sqlite --runtime-db runtime.sqlite
# Open http://127.0.0.1:8765/dashboard
```

When running the full `runtime_daemon.py` with `--observability-db`, the dashboard is served on its existing metrics port; no second server should bind that port.

Includes: health status, runtime goal aggregates, pending events, worker liveness, active alerts, and accumulated control cycles. Auto-refresh every 10 seconds and manual refresh.

**Limitations:** This is an **operational dashboard**, not an authenticated production control plane. Approval, goal modification, code execution, and credential management are deliberately absent. Reverse proxy access needs strong authentication, TLS, authorization, rate limits, and audit. Remote worker heartbeat does not imply distributed task dispatch. Real Docker and external SDK integration require deployment testing.
