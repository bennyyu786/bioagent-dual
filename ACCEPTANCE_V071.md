# v0.7.1 acceptance matrix

| Requirement | Offline status | Deployment status |
|---|---|---|
| v0.7.0 regression preserved | 282 tests passed including added operational tests | pending |
| HTTP health/live and ready | tested with local HTTP | pending deployment |
| Prometheus metrics / status redaction | tested | pending dashboards |
| Remote worker telemetry authentication | tested invalid/valid tokens | pending remote host |
| Worker stale alerts and cooldown | simulated time, tested | pending real worker |
| Runtime goal/event aggregation | tested with SQLite | pending real goals |
| systemd service template | static file delivered | not run as systemd |
| Dockerfile and Compose template | static files delivered | no Docker available for execution |
| Real Claude/Codex/Biomni/MCP smoke test | only presence checker | not performed |
| 24/7 uptime and HA | not claimed | unverified |

No production deployment is claimed. Dedicated sandbox/build hosts, telemetry transport protection, alert delivery and live SDK credentials remain deployment responsibilities.
