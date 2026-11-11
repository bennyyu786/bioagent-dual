# Biomni Control Center v0.8.1 — Interactive DAG and correlated trace

This is an additive upgrade to the single `biomni_dual/` codebase of v0.8.0.

## What works

- `/control` → **任务 DAG**: SVG workspace-authorized dependency graph, ranked from left to right, searchable/filterable, zoomable and scrollable. Click or keyboard-select a node to load its detail panel.
- `GET /api/v2/{workspace}/graph`: read-only nodes/edges and cycle/hidden-dependency metadata. Authorization is enforced by the same `CollaborationStore.tasks` filtering used by `/overview`.
- `GET /api/v2/{workspace}/tasks/{id}/trace`: read-only task status, attempted executions, capability IDs, error summary, local task-center state transitions and correlated collaboration events. Unknown or foreign task IDs return 404. Raw results and log text are deliberately excluded from this endpoint.
- Chat → Task: an authored chat message can be **explicitly** converted into a task. Backend checks that it belongs to the workspace, was authored by the operator, and the submitted task prompt matches its body; links to its source session and message ID are persisted.
- Optional `task_id` on authenticated remote Worker log submission: the server checks the referenced task belongs to that Worker's workspace before emitting the log event. Raw log text remains on the workspace-scoped log stream.
- Task → Chat/Logs: task detail offers navigation to its source conversation and the workspace log panel. This navigation does not claim untagged Worker logs are task-specific.
- WebSocket event pushes trigger dashboard refresh; normal polling remains as fallback.

## Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[server,test]'

# One-time initialization (local CLI only)
export BIOMNI_BOOTSTRAP_PASSWORD='replace-with-a-high-entropy-password'
python scripts/team_server.py --bootstrap-email admin@example.com
unset BIOMNI_BOOTSTRAP_PASSWORD

python scripts/team_server.py
```

Open `http://127.0.0.1:8766/control`, sign in and select **任务 DAG**.
For deployment outside localhost, use authenticated TLS reverse proxy and restrict administrative endpoints.

## Security boundaries

- Viewing graphs/trace is workspace-authorized. Graphs do **not** reveal missing, unauthorized or cross-workspace dependency IDs.
- Task messages are not automatically converted or executed. The user must confirm conversion in the browser.
- Trace intentionally excludes raw result artifacts, worker logs and event payloads. Sensitive details should be fetched through dedicated scoped endpoints.
- This release is **not** a distributed production-cluster acceptance. It retains v0.8.0's single-node SQLite state and role model.

## Verification

`PYTHONPATH=. pytest -q` exercises existing orchestration/collaboration tests plus DAG layout determinism, cycles, workspace isolation, source-message provenance, Worker log correlation, and source redaction. JavaScript passes `node --check`.

Chromium browser navigation to localhost is blocked by the execution environment policy (`ERR_BLOCKED_BY_ADMINISTRATOR`), so no actual browser visual verification was claimed.
