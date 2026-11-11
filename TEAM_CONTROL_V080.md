# Biomni Control Center v0.8.0 — Multiuser Collaboration

## Status and scope

This release supplies an **operational single-node, multiuser control-plane reference**. It is not certified as a production-grade, high-availability multi-node system. SQLite state, process-local event polling and restricted worker command clients are deliberate initial limits.

### Implemented

- FastAPI/ASGI multiuser server; password storage using salted `scrypt`, fixed-life (8h) hashed opaque bearer sessions, logout/revocation, login lockout, bootstrap from local CLI only.
- Workspaces with owner/admin/operator/viewer roles, membership changes/revocation, workspace-scope tasks, messages, events and audit records.
- Browser workbench: overview, task submission, team chat, Worker list/commands, WebSocket event/log feed, models, membership management. Works on desktop and narrow tablets.
- Real-time WebSocket delivery with **single-use 30-second tickets** issued over an authenticated API; subsequent membership checks during delivery. HTTPS reverse proxy must support WebSocket Upgrade.
- Dedicated Worker credentials; per-Worker polling and command receipt for allowlisted commands `drain`, `resume`, `inspect_job`, `cancel_job`. Command claim is atomic and **never automatically replayed** after loss of receipt.
- Worker telemetry/log events persist in SQLite (bounded and stripped of terminal control codes); browser subscribes per workspace.
- OpenAI-compatible model discovery and chat using server-side credential; model has no tools or task/approval privileges.
- Docker Compose + Caddy TLS template for one host, read-only container filesystem, non-root user, no-new-privileges and dropped capabilities.
- Security and integration tests including a real local HTTP model stub (not just a fake callable).

### Intentionally not claimed / deployment gates

- No production SSO/MFA/OIDC or corporate identity-provider integration, distributed RBAC policies, token refresh/rotation automation, cross-host session revocation cache.
- No PostgreSQL/Redis multi-replica durability, distributed event broadcast, log retention or continuous backup implementation. SQLite must be on one host and requires snapshots/backups.
- No full remote executable job scheduler/agent runtime. Reference Worker client handles `drain`/`resume`; `cancel_job`/`inspect_job` require integrating a trusted Worker supervisor. No arbitrary shell commands are accepted.
- No true independent per-tenant model secret vault; one `BIOMNI_MODEL_API_KEY` applies to configured model calls. Do not put tenants with distinct secrets on the same deployment without adding secret separation.
- No verified public-domain/TLS deployment, production model credentials, long-running load/chaos tests or remote Docker/Claude/Codex/Biomni end-to-end integration in this environment.
- No LLM-mediated permission change, automatic approvals or implicit task execution from chat.

## Install and local run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,test]'
export BIOMNI_BOOTSTRAP_PASSWORD='replace-with-a-strong-random-password'
python scripts/team_server.py --bootstrap-email admin@example.com
unset BIOMNI_BOOTSTRAP_PASSWORD
python scripts/team_server.py
```

Open `http://127.0.0.1:8766/control`. Local SQLite files default to `state/` (restrict directory permissions). Login with the bootstrap account. The API does not allow public bootstrap, and subsequent attempts to bootstrap are refused.

Worker registration (owner/admin only) occurs in the interface; copy its **one-time** secret to the remote host and run:

```bash
export BIOMNI_WORKER_SECRET='one-time-registered-worker-secret'
python scripts/worker_client.py --url https://biomni.example.org --id worker-01
```

`drain` and `resume` modify the sample worker's local scheduling intent. Actual jobs require a separate trusted executor. The reference client does not run arbitrary shell commands.

For Docker Compose on a single server:

```bash
cd deploy/team
cp .env.example .env
# Set BIOMNI_DOMAIN and secrets via your secret manager
# First bootstrap must run explicitly using the control image/volume as a one-off command.
docker compose up -d --build
```

**Before running `docker compose up`:** with Caddy, create a DNS A/AAAA record and open ports 80/443. When initializing the admin inside the control image, pass a strong `BIOMNI_BOOTSTRAP_PASSWORD` from your secret manager, then remove it. Use a stable application volume, backups, host access controls, TLS validation, Caddy/uvicorn log sanitization and external rate limiting. Caddy is a template, not a substitute for penetration testing.

## Access roles

| Action | Viewer | Operator | Admin | Owner |
|---|:---:|:---:|:---:|:---:|
| Read workspace overview, tasks, chat, event stream | yes | yes | yes | yes |
| Post chat, submit tasks | no | yes | yes | yes |
| Inspect Worker command history | no | yes | yes | yes |
| Register Worker, issue allowed commands | no | no | yes | yes |
| Member invite/role/revocation, model configuration | no | no | yes | yes |
| Remove workspace owner | no | no | no | no |

`ControlCenterServer` from v0.7.3 is a separate **legacy loopback-only operator interface**; do not publish it to the internet or treat its shared token as a multiuser identity.

## Useful APIs

- `POST /api/v2/login`, `POST /api/v2/logout`, `GET /api/v2/me`.
- `GET /api/v2/{workspace}/overview`, `POST /api/v2/{workspace}/tasks` (explicit submission with idempotency).
- `POST /api/v2/{workspace}/chat`, `GET /api/v2/{workspace}/chat/{session}`.
- `POST /api/v2/{workspace}/tickets`, `WS /ws/{workspace}?ticket=...`.
- `GET|POST /api/v2/{workspace}/members`, role change/remove, audit query.
- Worker `poll`, `ack`, `logs` endpoints use separate `X-Worker-Secret`.
- Model `config`, `probe`, `chat` endpoints. Server-side `BIOMNI_MODEL_API_KEY` is required for probe/chat.

Use TLS for all non-local access. Bearer credentials are deliberately retained **in page memory**, not browser localStorage. Reload requires another login. Do not log query strings containing one-time WebSocket tickets.

## Security/operations checklist

- Set `BIOMNI_DOMAIN`, valid DNS and TLS; disallow direct remote access to port 8766.
- Bootstrap only via local CLI, use high-entropy credentials, prohibit default passwords; set server-side secrets through vault/secret manager.
- Use protected SQL filesystem and volume backups with restore drills, key revocation procedures and retention policies.
- Workers must authenticate over TLS; deploy signed/versioned executable workloads via trusted supervisor (not implemented by reference client).
- Cap WebSocket/client counts at proxy; configure reverse proxy request size/rate limits and monitor failed auth.
- Validate data classification and whether prompts may leave your network before enabling external model providers.
- For production HA, replace the SQLite store and in-process event polling with PostgreSQL transactions and Redis/NATS/WebSocket fanout; ensure task ownership mapping and DAG writes share a transactional boundary.
- Run permission, cross-tenant, XSS, CSRF/CORS, SSRF, network fault, load/chaos, and credential rotation tests in the actual deployment.

## Quick verification

```bash
python -m pytest -q
node -e "const s=require('fs').readFileSync('biomni_dual/web/collaboration.html','utf8');new Function(s.match(/<script>([\\s\\S]*?)<\\/script>/)[1])"
python scripts/team_server.py --help
```

The test suite exercises HTTP access controls, websocket single-use tickets, SQLite persistence and real HTTP requests against a local stub model provider. These tests do not establish sustained operation against remote model vendors or real Workers.
