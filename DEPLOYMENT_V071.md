# v0.7.1 — Deployment and Observability

## What actually ships

* An operator-controlled **read-only HTTP status server** (Python standard library): `GET /health/live`, `GET /health/ready`, `GET /status`, `GET /metrics` (Prometheus text).
* A **telemetry-only** remote Worker heartbeat endpoint: `POST /internal/worker-heartbeat` with a pre-registered per-worker token. This **does not** enable remote task execution, transfer workloads or authenticate remote tool use.
* SQLite operational records (counters, service heartbeats, Worker last-seen timestamps, deduplicated/cooldown alerts), plus read-only aggregates from the v0.7.0 runtime database (goal states, event queue and number of decisions).
* Docker Compose and systemd deployment templates, a basic live acceptance checker, and regression/operational tests.
* An explicit opt-in container bind for `0.0.0.0`: exposed only via `127.0.0.1:8765` on the Docker host. Do not publish this directly to the internet. Use a controlled VPN, SSH tunnel or authenticated reverse proxy for remote monitoring.

## 1. Local operations test

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python examples/persistent_runtime_demo.py
python scripts/operations_cli.py --db /tmp/biomni-ops.sqlite register-worker biomni-worker-a
```

The register command prints a random token **once**, to the terminal. Store the token in your secrets manager. The database stores only SHA-256 of the token, so a database copy cannot be directly used as a Worker credential. Protect the database anyway.

Run a configured trusted factory (no default API keys or remote credentials are provided):

```bash
python scripts/runtime_daemon.py \
  --bootstrap config.runtime_factory:create_runtime \
  --observability-db /var/lib/biomni-dual/operations.sqlite \
  --reconcile-startup
```

Check:

```bash
curl -fsS http://127.0.0.1:8765/health/live
curl -fsS http://127.0.0.1:8765/health/ready
curl -fsS http://127.0.0.1:8765/metrics
python scripts/live_acceptance.py
```

Send a Worker heartbeat (over localhost or a mutually authenticated encrypted tunnel):

```bash
curl -fsS -X POST http://127.0.0.1:8765/internal/worker-heartbeat \
  -H 'Content-Type: application/json' \
  --data '{"worker_id":"biomni-worker-a","token":"YOUR_LONG_TOKEN"}'
```

Do not include secret tokens in shell history in production. Use a secret file or equivalent.

## 2. Docker Compose on a dedicated Linux host

The project has **no working production bootstrap by default**: create and review `deploy/docker/config/runtime_factory.py` from its `.example`, connect your approved TaskCenter, Planner, CapabilityCenter, SDK credentials and database paths, and verify that it returns `PersistentAutonomousRuntime`.

```bash
cd deploy/docker
cp config/runtime_factory.py.example config/runtime_factory.py
# Edit the trusted factory for your deployment (do not place API keys in the file).
mkdir -p state
sudo chown -R 10001:10001 state
# Invoke using the compose file, with context ../.. relative to the file.
docker compose up -d --build
curl -fsS http://127.0.0.1:8765/health/ready
```

The container starts as UID 10001, has read-only root filesystem, dropped capabilities, no-new-privileges, resource limits and a dedicated writable state directory. It does not mount `docker.sock` or host user secrets. For real Skill/MCP building, use a separate authorized Rootless Docker sandbox host rather than granting Docker daemon access to this orchestrator. The bind is restricted on the *host* side: `127.0.0.1:8765:8765`.

## 3. systemd

Create a dedicated `biomni` account and place the reviewed factory into `/opt/biomni-dual/config/runtime_factory.py`; configure the state directory. Review the unit file under `deploy/systemd/`, then:

```bash
sudo cp deploy/systemd/biomni-dual.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now biomni-dual
sudo systemctl status biomni-dual
journalctl -u biomni-dual -f
```

systemd uses `StateDirectory=biomni-dual`, a dedicated unprivileged account, filesystem protections and restart-on-failure. The trusted factory remains an explicit operator responsibility.

## 4. Operational contract / known limits

* `/health/live` means the HTTP server thread responds. `/health/ready` additionally requires a fresh runtime heartbeat; it is **not a biomedical correctness check**.
* Stale Worker detection uses heartbeat age only. A registered, heartbeat-capable worker is **not** proof that it can execute an assigned task; it is telemetry, not worker scheduling or a distributed lease.
* On restart, existing v0.7.0 reconciliation quarantines orphaned RUNNING tasks. External effects may require manual reconciliation; exactly-once remote execution is not guaranteed.
* SQLite is for a single host. These deployment files do not provide active-active HA, cross-host leases, secure remote task execution, persistent metric history or an external alert notification sink.
* Alert records are persisted and cooldown-limited; operators must poll `/status` or connect their own authenticated monitoring/notification system. No SMS/Matrix/email delivery is asserted.
* The HTTP server binds loopback by default and has **no credentials on read-only endpoints**. The container-wide bind is permitted only via `--allow-container-bind`; never use it on a public host without a protected ingress.
* Client-provided Worker tokens never appear in status or metrics. Protect token transport with TLS/VPN/SSH if crossing machines.
* This environment can test the Python service and configs, but cannot verify a real Docker daemon, model credentials, remote MCP, Biomni A1, or 24/7 uptime.

## 5. Acceptance requirements

1. Existing end-to-end/regression tests still pass.
2. HTTP live/ready/metrics endpoints respond and contain only non-sensitive aggregate information.
3. Invalid worker token rejected, and stale Worker status generates an alert.
4. Cooldown prevents alert floods; recovery resolves the alert.
5. Runtime event/goal counters are read-only and do not leak event payloads.
6. Restart preserves Worker registration, counters and alerts.
7. Compose/systemd templates are provided and reviewed; `docker compose config` and real daemon execution must run at the deployment host.
8. `scripts/live_acceptance.py` reports installed runtimes and probes health; a successful health check does not imply successful real Claude/Codex/Biomni operations.
