# Biomni Control Center v0.7.3

## What is delivered

A responsive, authenticated operator workbench built on the original `biomni_dual/` codebase. It is a Python HTTP service with a standalone HTML/JS frontend bundled as package data. It works without Node/npm or a CDN.

Screens: Overview; task dependency DAG and task inspector; Skill/MCP capability inventory; registered Worker health; remediation jobs; active experiments; approval inbox; and persistent conversation/explicit task submission.

The dashboard reads real persisted state. Empty tables are shown honestly when databases or components are not configured. No synthetic data or fabricated LLM answers are presented.

## Quick start

Install (Python >= 3.11):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
export BIOMNI_CONTROL_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(36))')"
python scripts/control_center_server.py --task-db biomni_dual.db \
  --ops-db operations.sqlite --conversation-db control.sqlite
```

**Save the token generated above** in a password manager or environment manager. It is not stored in a browser cookie, localStorage, or the HTML page. Enter it when the page prompts; it only stays in JS memory until reload.

Open `http://127.0.0.1:8765/control` on the same host. Other monitoring endpoints `/health/live`, `/health/ready`, `/metrics`, `/status` and `/dashboard` remain available.

To monitor existing runtime databases, use matching paths:

```bash
python scripts/control_center_server.py \
  --task-db /srv/biomni/state/tasks.sqlite \
  --ops-db /srv/biomni/state/ops.sqlite \
  --runtime-db /srv/biomni/state/runtime.sqlite \
  --conversation-db /srv/biomni/state/control.sqlite \
  --remediation-db /srv/biomni/state/remediation.sqlite \
  --experiments-db /srv/biomni/state/experiments.sqlite
```

Be sure the daemon and console share the same real task database. The console **does not start any runtime or worker**. It writes an explicitly confirmed task into `TaskCenter`; an authorized and configured orchestrator must separately bind the task to an active goal and schedule it. Thus "task submitted" is not "execution started".

### Optional real AI chat

Default chat replies honestly identify that no language model is connected. For a real response, configure an OpenAI-compatible `/v1/chat/completions` endpoint server-side:

```bash
export BIOMNI_CHAT_BASE_URL="https://your-trusted-model-service.example/v1"
export BIOMNI_CHAT_MODEL="your-model-id"
export BIOMNI_CHAT_API_KEY="your-api-key"
python scripts/control_center_server.py --task-db biomni_dual.db
```

No API key is returned to the browser. **Chat messages/history are sent to the configured endpoint**; configure only an endpoint approved for your research data. The advisor has no tools and cannot execute tasks, install capabilities, approve changes, or modify databases. All task submissions and approvals use separately authenticated control APIs.

### Live runtime integration

To mount the new console on the **same** observed service (not a competing server on port 8765), pass `control_center=your_control_center` to `serve_observed(...)` from trusted bootstrap code. Both components must use the same `OperationsStore` object. For remote access use TLS and an authenticated reverse proxy / SSH tunnel. `ControlCenterServer` refuses direct wildcard/interface bind.

## Operational routes

| Route | Auth | Action |
| --- | --- | --- |
| `GET /control` | Public static shell | HTML/JS, no private records |
| `GET /api/control/overview` | Bearer operator token | Real task DAG / ops / capabilities / repairs / experiments / approvals |
| `GET /api/control/messages?session=main` | Bearer operator token | Saved conversation |
| `POST /api/control/chat` | Bearer operator token | Store user/assistant messages; optional advisory LLM |
| `POST /api/control/submit-task` | Bearer operator token + explicit UI confirmation | Idempotent TaskCenter submission linked to message ID |
| `POST /api/control/approve` | Bearer operator token + explicit confirmation | Task approval or capability proposal review |
| `POST /api/control/goal-state` | Bearer operator token + explicit confirmation | Pause/resume existing runtime goal |

The read-only aggregate `/api/dashboard` remains unauthenticated on **loopback only** as in v0.7.2; it does not contain task prompts or chat messages. The private overview endpoint does include task prompts, so it must never be exposed without authentication.

## Safety and correctness

- `BIOMNI_CONTROL_TOKEN` >= 32 characters. For deployment use a randomly generated high-entropy token, not a human password.
- Localhost bind by default. HTTP POST must be authenticated, same-origin if an Origin header is sent, and JSON with an 8 KB size cap. The web application uses DOM `textContent` rather than embedding user-controlled HTML.
- Conversation messages persist in `control.sqlite` (sensitive research content may be present). Restrict file permissions and backups; use encryption at rest if required.
- Task submission is deliberate, idempotent via the original `TaskCenter` mechanism; no message is automatically executed.
- Task approval returns a task to pending; it does not bypass other guards. Capability proposal approval is **not** capability publication or verification.
- Goal pause/resume uses the existing `PersistentRuntimeStore` operator state machine. No direct stop/resume of uncertain external side effects is allowed.
- Repair and experiment panels are observation-only. Real audit and verification remain in their established respective services.
- Long-running model calls currently use synchronous request handlers with a bounded timeout, not streaming or cancellation. This is a single-operator control prototype, not a hardened internet-facing multi-tenant service.

## What remains for production

Multi-user RBAC, CSRF/session-hardening behind an identity provider, WebSocket event streaming, real Worker task dispatch, a tested reverse proxy/TLS setup, executor-side authorization, load tests, and live Claude/Codex/Biomni provider integration. These are not claimed as completed.

## Verification

```bash
python -m pytest -q
python -m compileall -q biomni_dual scripts
```

The full test suite includes authentication rejection, cross-session task isolation, chat persistence, duplicate task submissions, task DAG and approval state, capability approval boundary, goal lifecycle, malformed requests, and read-only auxiliary database projections.
