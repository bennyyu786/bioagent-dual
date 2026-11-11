# v0.8.4.post1 — Web task submission / automatic dispatch bridge

## Root cause

The `/api/v2/{ws}/tasks` endpoint used to create tasks without
`required_capabilities`, whereas the resource-aware dispatcher requires exactly
one independently approved executable capability. The web task form did not
connect to the dispatcher. This produced indefinitely pending tasks with zero
attempts, even when the chat model URL and API key were configured.

## Fixed behavior

1. An administrator first approves a real executable capability in the workspace
   and installs a corresponding allowlisted adapter on a registered Worker.
2. Configure that Worker's resource capacity and capability IDs; run the worker
   client so authenticated heartbeat/poll indicates it is online.
3. Open Control Center -> Task DAG -> Automatic execution policy (administrator).
   Select the *already approved* default capability and a bounded resource
   request; enable the policy and save.
4. Subsequent user task submissions are given that explicitly configured
   capability. The server immediately attempts to create a resource-aware
   dispatch offer. The returned JSON includes `dispatch.state` and, where
   applicable, `dispatch.reason`.
5. If no eligible Worker is online, the authenticated Worker poll will re-check
   pending tasks that match the approved policy. This is idempotent.
6. To run pre-patch tasks that had no capability, select a pending task in Task
   DAG and click **Activate / Redispatch** as an administrator. This action
   binds the previously approved capability atomically and attempts dispatch.

## Security

- Policy changes and activation require workspace admin privileges.
- Enabling the policy requires independently recorded capability approval.
- Submission does not auto-approve unknown capabilities or infer arbitrary
  execution commands from user-controlled prompt text.
- Worker claims, result reports, and independent review stay as in v0.8.4.
- Existing unclaimed tasks are not silently converted to executable jobs.
- No tasks run unless a real Worker is deployed with an approved local adapter.
- This is a safe default-capability dispatch fix, **not** yet an LLM-generated
  dynamic DAG or automated Skill/MCP construction pipeline. That integration
  still requires explicit approved runtime configuration.

## API

- `GET /api/v2/{workspace}/execution-policy` (viewer+)
- `PUT /api/v2/{workspace}/execution-policy` (admin+)
- `POST /api/v2/{workspace}/tasks` (operator+) now returns `dispatch`
- `POST /api/v2/{workspace}/tasks/{id}/activate` (admin+; old pending tasks)
- `POST /api/v2/{workspace}/tasks/{id}/auto-dispatch` (admin+)

Example policy, using a capability that has **already** been reviewed and
approved, with a Worker configured to execute it:

```json
{
  "enabled": true,
  "capability": "analysis",
  "resources": {"cpu_milli": 1000, "memory_mb": 1024, "gpu_units": 0}
}
```

This is a *resource accounting request*, not a real CPU/GPU sandbox limit.

## Upgrade

Replace source files from v0.8.4 with this full archive (or reinstall the
whole package). Preserve your existing `state/` directory and all server
secrets; do not replace the deployment databases with empty ones. Restart
`python scripts/team_server.py` or recreate your container as appropriate.
SQLite adds `workspace_execution_policy` on first boot without dropping
existing tables.

Do not insert API tokens or Worker credentials into the web frontend.

## Verification

```
python -m pytest -q
python -m pytest -q tests/test_submission_autodispatch_fix.py
```

Existing test suite 333 + patch tests 5 = 338 passing locally. Actual external
Worker/provider execution requires credentials and deployed adapters.
