# Biomni Dual Center v0.9.0 — Adaptive Routing and Automatic Delivery

Built directly on the v0.8.9 **auto-delivery** source package.

## Features

- Existing automatic result delivery is retained: Worker `report` triggers durable success/failure transition, releases resource reservation, emits `task_completed`/`task_failed`, and the Web UI retrieves `/tasks/{id}/result`. **No human result review click is required.** Previously uncertain results remain quarantined, not auto-approved.
- `biomni_dual/adaptive_router.py`: optional **read-only/no-tools** Claude/Codex SDK/CLI classification for ambiguous requests. Outputs `simple`, `single`, or `complex`, with conservative fallback on timeout, malformed output, or unavailable backend. Explicit side-effecting commands cannot be downgraded to simple.
- Workspace-admin endpoints `GET/PUT /api/v2/{ws}/routing-policy` accept `{"enabled":true,"backend":"codex_sdk"}` (alternatives: `claude_sdk`, `codex_cli`, `claude_cli`). This is configured separately from the existing planning policy. Models/CLIs must actually be installed and authenticated on the server; otherwise the system falls back to safe rules.
- Control Center's task page now includes **自适应任务判断** settings alongside planning settings.
- Task submission response includes `route` and `routing_source` for diagnostics. A simple task still requires approved execution capability and authorized Worker, as before.

## Configuration

```bash
pip install -e '.[server,test,agents]'
python scripts/team_server.py
```

For a workspace, configure the approved execution policy and enable the planning policy. Then in Web Control Center > DAG/Tasks > 自适应任务判断 enable the desired backend. When disabled (default), v0.8.9 conservative rules remain in use. These settings are persisted in `workspace_routing_policy` in the existing collaboration database; no database reset is necessary.

Important: SDK/CLI routing is **an additional model request**, not a built-in SDK complexity classifier. The API is authenticated through the same SDK/CLI environment as the planner; it never receives tools or execution authorization. Routing decisions never bypass RBAC, capability approvals, Worker registration, or resource controls.

The prior release's legacy manual review endpoint remains for historical records/compatibility only; new successful Worker reports use automatic delivery by default.

## Verification

- `pytest -q tests/test_v090_adaptive_router.py tests/test_v089_routing.py tests/test_auto_delivery_v089.py tests/test_web_planning.py` — 24 passed.
- Full suite was attempted but exceeded this execution environment's timeout at about 76%; no claim of complete suite success is made.
- No live Claude/Codex SDK, CLI, remote Worker or model service was exercised in this sandbox.

## Deployment warning

Do not copy over `state/`, `.env.service`, existing passwords, or local `team_server.py` port changes when updating an existing machine. The standard `team_server.py` retains its packaged defaults; keep your own port 3001 patch when merging.
