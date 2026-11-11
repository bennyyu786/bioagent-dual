# v0.4.1 Reliability design

This release evolves the **single main package** `biomni_dual/` in place. There is no `v03/`, `v04/`, or `v041/` source tree.

## Session checkpoints

`SessionStore` persists `(task_id, runtime, session_id, attempt_id, status, result)` in SQLite. `SDKWorker` checkpoints the SDK's returned session ID and the output. Calls that finished successfully return the cached result on repeated invocation. The SDK runtime is selected by an explicit `runtime_name` so that sessions are never silently resumed with another provider. `ClaudeSDKRuntime` passes the saved session ID through Claude Agent SDK's `resume` option; `CodexSDKRuntime` calls `thread_resume`.

**Crash limitation:** if the agent process or coordinator dies before the SDK returns a session ID, the remote side may still have performed an action. On restart, `SessionStore.recover()` changes RUNNING to UNCERTAIN; `TaskCenter.recover_interrupted(uncertain_ids)` changes those tasks to BLOCKED. An operator must reconcile external side effects before calling `SessionStore.reconcile(task_id, retry_allowed=True)` and `TaskCenter.reconcile_blocked(task_id, retry_allowed=True)`. No automatic exactly-once execution is claimed. Other interrupted tasks without uncertain sessions may be requeued under their attempt budget.

## Idempotency

- `TaskCenter.submit(..., idempotency_key=...)` atomically deduplicates repeated submissions; reusing a key with a different payload raises `ValueError`.
- `TaskCenter.claim()` atomically checks dependencies and changes PENDING -> RUNNING using `BEGIN IMMEDIATE`. Multiple coordinators cannot successfully claim the *same* task simultaneously, though the overall scheduler still uses an in-memory resource pool and is not a distributed scheduler.
- `TaskCenter.finish()` atomically commits completion and accepts identical repeated successful completions without adding an event.
- `BuildStore.create(..., idempotency_key=...)` and `SandboxBuilder.propose(..., idempotency_key=...)` deduplicate matching candidate builds.
- `SandboxBuilder.publish(build_id)` returns the same previously published verified capability on a repeated call, without creating a new version.

Idempotency keys prevent coordinator duplicates; they do not guarantee exactly-once execution of arbitrary external tools or all side effects. For distributed operation use resource leases, fencing tokens, and remote service idempotency contracts.

## Structured error classification

`classify_error()` returns `ErrorInfo(kind, retryable, code, message)`. Non-retryable authentication/permission/input/dependency failures stop immediately; timeouts, transient networking, rate limits, resource exhaustion and unknown errors may retry up to `max_attempts`. Unknown is deliberately treated as retryable for backward compatibility and should be tuned for production. There is currently **no backoff or retry-after policy**; avoid running at high scale without it. Secrets and raw worker outputs must not be placed in error messages.

## Audit log

`AuditLog` stores event time, actor, action, subject and minimal metadata, linked by SHA256 hashes. Task state writes and audit entries share one SQLite transaction. Build proposal, transition and publication events are also logged transactionally. `AuditLog.verify()` detects edits to hash-linked records but **cannot detect deletion of final entries, a malicious actor who rewrites the entire chain, or database destruction** without periodic off-host anchoring. Use managed immutable audit storage, signatures and access controls for production compliance. Audit logs intentionally avoid full user prompts and raw tool output; old `events` / build evidence still contain some detailed text and should be protected and redacted separately.

## Local run

```bash
pip install -e '.[test]'
python -m pytest -q
python examples/reliability_demo.py
```

Docker, Claude/Codex credentials, Biomni A1, and real MCP protocol integrations remain deployment-dependent and were not live-tested in this container.
