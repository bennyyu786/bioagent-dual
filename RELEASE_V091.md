# Biomni Dual Center v0.9.1 — Unified conversation homepage

This release builds on the v0.9.0 automatic-delivery source tree. The Python package remains `biomni_dual/` (no parallel version-specific package).

## Highlights

- Reference-inspired light, minimal homepage: conversation history, single rounded composer, modes (auto / chat-only / run task), send action, optional browser speech recognition, new conversation. The `+` button expands the existing professional navigation rather than pretending file-upload support exists.
- One workspace-scoped API `POST /api/v2/{ws}/assistant/turn` intelligently chooses **text-only model chat** or the already-authorized Task Center based on the user's selected mode and conservative rules. It does not grant any additional tool privileges.
- `GET /api/v2/{ws}/assistant/session/{session}` joins workspace conversation messages with task states, and shows result text **only after `completed`**; task metadata remains linked to the original message.
- Task Center/DAG, Capability/Topology Center, Worker Center, logs, model setup and member management remain accessible under the collapsed “专业视图 / 管理” sidebar.
- Existing manual-result-review APIs remain for historical compatibility; normal execution uses the prior v0.8.9 auto-delivery flow.
- User-provided text is HTML escaped when rendered. Current model chat does not have tools and cannot execute shell commands.

## Deployment

Use your existing configuration and database; do not overwrite `.env.service`, `state/`, or local `team_server.py` port customizations.

```bash
python3 -m pip install -e '.[server,test,agents]'
python scripts/team_server.py
```

If your local service runs on port 3001, preserve your existing startup configuration and visit `/control` on port 3001. For basic conversation the workspace must have a model configured under **专业视图 → 模型连接**, and `BIOMNI_MODEL_API_KEY` must be set in the *server process* environment.

For tasks, the existing execution policy, capability approval, Worker registration and resource checks still apply. A submitted task may remain pending until an authorized Worker can execute it. The UI does not fabricate an answer while work is pending.

## Test and verification status

- `PYTHONPATH=. pytest -q tests/test_v091_unified_conversation.py tests/test_v089_routing.py tests/test_v090_adaptive_router.py`: **13 passed**.
- JavaScript syntax check: **passed** (extracted inline JS checked with Node).
- Full-suite and a broader integration test run were **interrupted by execution-environment timeouts**, so complete regression is **not claimed**.
- Real Claude/Codex, LiteLLM, remote Worker and browser speech recognition need verification in your deployed environment.

## Important current limits

- The `+` control currently opens professional navigation; file attachment APIs were not added in this version.
- Speech input uses the browser's built-in Web Speech recognition when present; it is hidden in browsers without support.
- Chat model calls are read-only; system-state questions may be routed to the Task Center instead of hallucinating live system data.
- API idempotency and exactly-once semantics for conversation turns across network failures require further hardening for production multi-process deployment.
