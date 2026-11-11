# Biomni Dual Center v0.8.5 — Web auto-planning integration

This release extends v0.8.4.post1 **in the same `biomni_dual/` package**. It closes the user-facing gap from natural-language submission to a validated task graph and bounded dispatch using *preapproved* worker capabilities.

## Admin setup

1. Install server dependencies: `pip install -e '.[server]'`.
2. For Codex SDK planner install `pip install -e '.[codex]'` and configure Codex authentication **for the server process**; for Claude SDK install `pip install -e '.[claude]'` and configure Claude Agent SDK authentication. These are separate from the `BIOMNI_MODEL_API_KEY` used for chat.
3. Start `python scripts/team_server.py` with the existing production database paths. Back up `state/` first.
4. Register a real Worker with an independently reviewed, locally installed execution adapter, declare its capability ID and set its resource capacity. The Worker must authenticate and poll for jobs.
5. In **任务 DAG → 自动执行策略**, choose an already approved capability (e.g. `analysis`), set CPU/RAM/GPU budget, and turn on automatic dispatch. This setting **does not create or approve** capabilities.
6. In **任务 DAG → Claude/Codex 自动规划**, select `codex_sdk` or `claude_sdk` and enable auto planning. Optional CLI modes (`codex_cli` read-only or `claude_cli` with tools disabled) require their executables and authentication; CLI protocol compatibility depends on installed CLI release.
7. Decide whether to enable **默认批准已审核能力上的安全任务步骤**. This only removes redundant *per-step* approval for tasks mapped to the already approved capability. A task explicitly flagged for review or matching a high-risk-operation check still waits for approval. These checks are defense in depth, not a complete authorization engine; the Worker must enforce its own sandbox and capability allowlist.
8. Submit a goal from **任务 DAG** or **协作对话 → 转为任务**. An AgentRuntime call is made with no writable workspace; its proposed JSON is bounded to eight steps, restricted to the current workspace and one approved capability, and committed through DAGVersionManager. The original goal is the final synthesis task, dependent on all new steps. Ready steps are offered to eligible Workers.

## API

| Route | Method | Purpose |
| --- | --- | --- |
| `/api/v2/{ws}/planning-policy` | `GET` | Read workspace policy |
| `/api/v2/{ws}/planning-policy` | `PUT` | Admin sets `enabled`, `backend`, `auto_approve_trusted` |
| `/api/v2/{ws}/tasks` | `POST` | Submit and, if enabled, synchronously plan/dispatch |
| `/api/v2/{ws}/tasks/{id}/planning` | `GET` | Scoped planning state and step IDs |
| `/api/v2/{ws}/tasks/{id}/replan` | `POST` | Admin retries a cleanly failed plan only |

### Server-side states

- `planned`: validated DAG committed and execution capability bound to goal. Some steps may still wait for Workers or prerequisites.
- `planning`: another request is planning this goal; submission is idempotent.
- `failed`: missing SDK, authentication error, invalid JSON, invalid capabilities/dependencies, timeout, or other clean failure. The goal remains **unbound and not executable**. Admin can retry after fixing the backend.
- `reconcile_required`: DAG version may be committed but the final bookkeeping step failed; **no automatic retry**. Inspect the DAG and manually reconcile before proceeding.

The planner is called inline, bounded by its request timeout (currently 45 s). This is a single-node integration; dedicated background planning workers with heartbeat and crash recovery are a later hardening step.

## Guarantees and limitations

- Repeated request IDs return the same goal and cached plan; never generate another DAG.
- No implicit approval of *new* Skill/MCP capabilities. Publishing a capability is still separately gated.
- Planner runtime receives a read-only planning role; no approved code-writing workspace. Claude CLI tool disablement and Codex read-only sandbox depend on the installed provider tooling; validate in your own environment.
- No automatic replay after uncertain partial commits.
- The model API Key from the Web model-connection panel does **not** configure Claude/Codex SDK/CLI authentication.
- Real external Claude/Codex credentials, remote Worker processes, model API quality, and multi-machine load tests have **not** been validated in this environment. The 349 automated tests use deterministic test runtimes where appropriate.
