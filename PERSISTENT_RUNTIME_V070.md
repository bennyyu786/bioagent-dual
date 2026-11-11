# Persistent Autonomous Runtime v0.7.0

This release adds a *long-running controller around*, not in place of, the v0.6.7
`AdaptiveController`. Source remains one `biomni_dual/` package without version subfolders.

## Operational flow

1. `EnvironmentMonitor` samples explicitly allowlisted read-only probes. The
   first observation establishes a baseline; transitions are hashed to an
   event without copying the sampled data into the queue.
2. `AuthenticatedWebhookIngress` checks the HMAC and stable nonce; the caller
   must serve it over TLS with request-size, replay-retention and rate limits.
3. `PersistentRuntimeStore` stores deduplicated events, durable schedules,
   per-goal cooperative leases, lifecycle states and hash-chained audit entries.
4. `CadencePolicy` routes failures/completions to the fast lane, data/resource
   changes to medium, and timer events to slow; intervals throttle redundant
   *controller decisions* and do not grant execution privileges.
5. `PersistentAutonomousRuntime.step` obtains a lease, optionally stores a
   structured environment observation, runs one existing rolling decision,
   records audit, acknowledges the event and updates goal state.
6. `PersistentAutonomousRuntime.serve` loops until stopped. Configure a service
   manager such as systemd or Kubernetes for process supervision and restart.

### Invariants

- Not every environment change causes a new task. The existing planner decides
  what to do based on goal state and evidence, with DAG validation and limits.
- One event is *at-least-once*, not exactly-once. A process crash between DAG
  commit and event acknowledgment can redeliver an event. The DAG/TaskCenter
  idempotency and review gates remain necessary, and a model-generated plan
  must be reconcilable. No distributed exactly-once claim is made.
- SQLite leases are *cooperative single-host locks*, **not distributed
  fencing**. If a lease is lost mid-run, the runtime raises an error. Remote
  tool side effects could already have happened. Do not run separate machines
  against a network-shared SQLite DB. Use a transactional external queue and
  fencing-aware workers for multi-host operation.
- Startup recovery **quarantines** orphaned RUNNING tasks via
  `reconcile_startup`; never blindly calls `recover_interrupted` for potentially
  side-effecting Claude/Codex/MCP work. An operator must reconcile external
  effects and explicitly resume.
- Waiting approval, paused and stopped states prevent further automatic ticks.
  The `resume_goal(..., operator=True)` API refuses resumption while blocked,
  running or awaiting-approval tasks remain.
- The webhook ingress does not execute content or treat event body as a prompt.
- `FileFingerprintProbe` samples path metadata only and rejects paths outside
  a specified trusted root; file content, secrets or clinical data do not flow
  into event storage.
- Agent generated code remains behind existing Skill/MCP Docker verification,
  permission and approval boundaries. This release does **not** change that.

### Scheduling and budgets

- `PersistentRuntimeStore.schedule(...period_seconds=...)` emits an event when due and
  skips backlog after an outage to prevent catch-up storms. The service runs
  schedules only when the corresponding goal is active.
- Cadence is logical rather than rigid clock-driven: severe failures can bypass
  longer replan intervals. The existing `AdaptiveStore.max_cycles` and planner
  budget still cap the number of decisions, and a configured idle interval
  bounds polling overhead.
- A real worker may execute for longer than its lease; the runtime renews the
  lease while awaiting `tick`. External workers must nevertheless implement
  cancellation and idempotency themselves.

## Practical examples

```bash
pip install -e '.[test]'
pytest -q
python examples/persistent_runtime_demo.py
```

For a persistent process, write a **trusted local** factory that constructs the
real `TaskCenter`, `AdaptiveStore`, `AdaptiveController`, and
`PersistentAutonomousRuntime`, then run:

```bash
python scripts/runtime_daemon.py --bootstrap your_module:create_runtime --reconcile-startup
```

Do not use an untrusted module as a bootstrap factory. The daemon is an opt-in
reference wrapper, not a user-facing production API service or an authentication
server. Supply metrics collection, a reverse proxy, secure secret management,
process supervision, alerting, database backups, and remote SDK integration in
actual deployment.

## Validation scope

All existing 251 unit tests plus new deterministic runtime tests were executed.
This environment did **not** verify a long-running systemd service, live
Claude/Codex sessions, production Biomni A1, external MCP Servers, remote
Docker or multi-host failover. These remain deployment acceptance gates.
