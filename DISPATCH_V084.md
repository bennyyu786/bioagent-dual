# v0.8.4 — Resource-aware dispatch and remote worker execution

This upgrade extends the **same `biomni_dual/` source package** used by v0.8.3.

## Delivered behavior

- Workspace-scoped capability registration on Worker, separate from versioned capability approval. Both must be present before scheduling.
- `POST /api/v2/{ws}/dispatch/next`: administrator-authorized selection from `TaskCenter.ready()` (priority + aging), then selection of online capability-compatible Worker with sufficient reserved CPU/RAM/GPU allowance, with deterministic lowest-load tie-break.
- Atomic reservation **and** dispatch-offer insertion in a single SQLite `BEGIN IMMEDIATE` transaction, idempotent request key; one active reservation per task.
- Authenticated remote Worker `poll → claim → report` protocol. `claim` integrates with `TaskCenter.claim`; ambiguous cross-DB failures are **quarantined**, not automatically replayed.
- Worker reports transition to `review_required`; the reservation remains in `reported` state and continues consuming capacity until reviewed. A Worker self-report is **not scientific verification**.
- `POST /api/v2/{ws}/dispatch/{id}/verify` can invoke a server-installed trusted synchronous `(task,result)->(bool,evidence_ref)` validator. If absent, endpoint returns 503. Manual admin review is possible with result SHA256 and an independent evidence reference, but **manual attestation is not automatically verified scientific evidence**.
- Worker adapter is an explicit on-disk, locally trusted mapping of *one capability* to an absolute executable path and fixed argv. No shell, executable path, arbitrary CLI or model-supplied command is accepted from the API. JSON request is sent on stdin. Configure the process's OS account, network access, filesystem sandbox and resource limits separately.
- Worker lease expiry is **quarantined**; no auto release/retry if job status could be unknown. Unclaimed offers may be cancelled safely.
- Existing WebSocket event stream now includes dispatch lifecycle events, and topology includes `dispatch` relations and a reviewer panel. Priority dispatch, skill registration, resource review controls are in the existing UI.

## Start control plane

```sh
python -m pip install -e '.[server,test]'
python scripts/team_server.py
# Login http://127.0.0.1:8766/control
```

1. Admin creates Worker under Worker Center and saves its **one-time** secret securely.
2. Admin configures CPU/RAM/GPU capacity and declares the approved adapter's capability under 双中心拓扑.
3. Admin approves the capability lifecycle via `PUT /api/v2/{ws}/runtime/capabilities/{id}`, with valid verification evidence reference.
4. Submit a task with explicit `required_capabilities`. Basic chat-task submission has no such requirement by default, so it is intentionally not automatically executable until planned/assigned to an approved capability.
5. Ensure a remote Worker has polled successfully (online heartbeat). Admin uses '自动选择最高优先级就绪任务', or calls the dispatch API.
6. Worker polls, claims and runs a locally allowlisted adapter, then reports output. Admin or separately installed independent validator reviews output.

## Remote worker process (dedicated isolated host/container)

Create a local JSON file that only the Worker host administrator may edit:

```json
{
  "analysis": ["/usr/bin/python3", "/opt/biomni/trusted_analysis_adapter.py"]
}
```

Then start (example HTTPS endpoint):

```sh
export BIOMNI_WORKER_SECRET='ONE_TIME_SECRET_FROM_ADMIN'
python scripts/remote_worker.py \
  --server https://biomni.example.org \
  --worker lab-worker \
  --adapters /etc/biomni/adapters.json
```

For strictly local testing only, `--allow-http-loopback` enables `http://127.0.0.1:8766`; nonloopback HTTP and redirects are rejected. Secret is never sent to the executable adapter's environment; use separate narrowly scoped per-adapter credentials. Use TLS termination, secret rotation, network confinement, non-root execution, egress limits, host quotas and sandbox policies in production.

The example adapter receives JSON `{task_id,prompt,capabilities}` on stdin and returns a UTF-8 output string on stdout. A single capability is currently supported. Multi-capability tasks require an **explicitly approved composite adapter**; they are not executed by concatenating commands.

## New API routes

| Path | Authorized role | Function |
|---|---|---|
| `GET /api/v2/{ws}/dispatch` | member | Workspace dispatch status (no raw results) |
| `PUT /api/v2/{ws}/dispatch/workers/{worker}/skills` | admin | Declare Worker adapter capabilities |
| `POST /api/v2/{ws}/dispatch` | admin | Select Worker for specific eligible task |
| `POST /api/v2/{ws}/dispatch/next` | admin | Select next highest-priority eligible task |
| `GET /api/v2/worker/{worker}/dispatch/poll` | Worker secret | Poll offer and heartbeat |
| `POST /api/v2/worker/{worker}/dispatch/{id}/claim` | Worker secret | Claim resource lease + task |
| `POST /api/v2/worker/{worker}/dispatch/{id}/report` | Worker secret | Submit bounded result for review |
| `GET /api/v2/{ws}/dispatch/{id}/evidence` | admin | View untrusted raw output + SHA256 |
| `POST /api/v2/{ws}/dispatch/{id}/review` | admin | Human acceptance/rejection with evidence ref |
| `POST /api/v2/{ws}/dispatch/{id}/verify` | admin | Run configured independent trusted validator |
| `POST /api/v2/{ws}/dispatch/{id}/cancel` | admin | Cancel never-claimed offer |
| `POST /api/v2/{ws}/dispatch/scan` | admin | Quarantine timed-out execution leases |

## Safety and consistency notes

**TaskCenter SQLite and relation ledger SQLite are different databases:** they do not form an XA transaction. If an interrupt happens between task claim and durable dispatch update, or between reviewed TaskCenter completion and ledger release, the operation can remain `uncertain` / `reviewing`. These states require supervised reconciliation, not automatic re-execution. For multiple dispatcher instances use a real transactional SQL coordinator and fenced Worker tokens rather than relying on SQLite-only cooperative leases.

The Worker `reported` state holds resources pending review. This is a *quota ledger*, not sampled hardware CPU/RAM/GPU utilization. Execution leases do not currently support an explicit heartbeat extension; long-running jobs must finish before the configured lease TTL or be quarantined. Don't use this implementation for multi-hour GPU jobs without implementing fenced renewals and a durable Worker-side journal.

Capability `approved` is a registry/admin approval state, not live reachability. The trusted `result_validator` hook is not configured by default; in a real deployment inject a vetted domain-specific implementation via a custom ASGI bootstrap calling `create_app(..., result_validator=your_validator)`.

This host verified a local adapter and ASGI TestClient integration, **not** a remote Claude Code/Codex/Biomni SDK, production Kubernetes/Docker, physical GPUs, enterprise SSO, high availability or 7×24 production operation.
